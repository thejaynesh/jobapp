"""
A provider that takes one request at a time, in an app that doesn't send them
one at a time.

The worker runs two Celery processes, matching deliberately overlaps document
generation, and a fetch cycle expands its queries with an LLM call of its own.
So an endpoint capped at one concurrent request will refuse callers, and a
refusal reads exactly like the provider being broken. These tests are about the
queue that turns the second caller's error into a wait.
"""

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from app.llm.providers import Provider, call_provider, configured_providers, matching_fallbacks
from app.services import llm_gate


class FakeRedis:
    """Enough of Redis for a single-holder lock, with real mutual exclusion."""

    def __init__(self):
        self.store: dict = {}
        self.lock = threading.Lock()

    def set(self, key, value, nx=False, ex=None):
        with self.lock:
            if nx and key in self.store:
                return None
            self.store[key] = value
            return True

    def get(self, key):
        return self.store.get(key)

    def eval(self, script, numkeys, key, token):
        with self.lock:
            if self.store.get(key) == token:
                del self.store[key]
                return 1
            return 0

    def exists(self, key):
        return 1 if key in self.store else 0

    def ttl(self, key):
        return 150 if key in self.store else -2


@pytest.fixture
def fake_redis(monkeypatch):
    client = FakeRedis()
    monkeypatch.setattr(llm_gate, "_client", lambda: client)
    return client


class TestTheGate:
    def test_one_caller_gets_straight_through(self, fake_redis):
        with llm_gate.hold("fi", wait=1):
            assert fake_redis.exists("jobapp:llm:gate:fi")

    def test_it_is_released_afterwards(self, fake_redis):
        with llm_gate.hold("fi", wait=1):
            pass
        assert not fake_redis.exists("jobapp:llm:gate:fi")

    def test_released_even_when_the_call_blows_up(self, fake_redis):
        with pytest.raises(RuntimeError):
            with llm_gate.hold("fi", wait=1):
                raise RuntimeError("provider returned 500")
        assert not fake_redis.exists("jobapp:llm:gate:fi")

    def test_a_second_caller_waits_rather_than_being_refused(self, fake_redis, monkeypatch):
        # The whole point: the second request queues instead of erroring.
        monkeypatch.setattr(llm_gate, "_POLL_INTERVAL", 0.01)
        order = []

        def first():
            with llm_gate.hold("fi", wait=5):
                order.append("first-in")
                time.sleep(0.2)
                order.append("first-out")

        def second():
            time.sleep(0.05)
            with llm_gate.hold("fi", wait=5):
                order.append("second-in")

        threads = [threading.Thread(target=first), threading.Thread(target=second)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert order == ["first-in", "first-out", "second-in"]

    def test_waiting_forever_is_not_an_option(self, fake_redis, monkeypatch):
        # A worker slot blocked behind a wedged lock is worse than one paid call
        # somewhere else, so the wait has a ceiling and the caller is told.
        monkeypatch.setattr(llm_gate, "_POLL_INTERVAL", 0.01)
        fake_redis.set("jobapp:llm:gate:fi", "someone-else")
        with pytest.raises(llm_gate.Busy):
            with llm_gate.hold("fi", wait=0.05):
                pass

    def test_it_does_not_release_a_lease_that_expired_under_it(self, fake_redis):
        # After a TTL expiry the holder is not the owner any more, and deleting
        # then would open the gate in the middle of someone else's call.
        with llm_gate.hold("fi", wait=1):
            fake_redis.store["jobapp:llm:gate:fi"] = "a-later-holder"
        assert fake_redis.get("jobapp:llm:gate:fi") == "a-later-holder"

    def test_redis_being_down_does_not_stop_all_llm_work(self, monkeypatch):
        # The gate avoids a 429 from a provider that already says it is busy.
        # Refusing every call because the lock service is down trades a
        # recoverable error for a total outage.
        def explode():
            raise ConnectionError("redis unreachable")

        monkeypatch.setattr(llm_gate, "_client", explode)
        ran = []
        with llm_gate.hold("fi", wait=1):
            ran.append(True)
        assert ran == [True]

    def test_different_providers_do_not_block_each_other(self, fake_redis):
        with llm_gate.hold("fi", wait=1):
            with llm_gate.hold("other", wait=1):
                assert fake_redis.exists("jobapp:llm:gate:other")


class TestGatingIsAppliedWhereItMatters:
    def _provider(self, max_concurrency=1):
        return Provider(
            name="freeinference", api_key="k", model="glm-5.1",
            base_url="https://freeinference.example/v1",
            max_concurrency=max_concurrency, paid=False,
        )

    def test_a_single_slot_provider_goes_through_the_gate(self, fake_redis):
        held = []
        with patch("app.llm.providers._call_openai_compatible",
                   side_effect=lambda *a, **k: held.append(
                       fake_redis.exists("jobapp:llm:gate:freeinference")) or "out"):
            call_provider(self._provider(), [{"role": "user", "content": "hi"}])
        assert held == [1], "the gate must be held while the call is in flight"

    def test_an_unlimited_provider_does_not_pay_for_a_gate(self, fake_redis):
        with patch("app.llm.providers._call_openai_compatible", return_value="out"):
            call_provider(self._provider(max_concurrency=0),
                          [{"role": "user", "content": "hi"}])
        assert not fake_redis.store

    def test_a_gate_that_never_opens_looks_like_any_other_failure(
        self, fake_redis, monkeypatch
    ):
        # So the caller's existing failover chain handles it, rather than
        # needing to know this provider is special.
        monkeypatch.setattr(llm_gate, "_POLL_INTERVAL", 0.01)
        monkeypatch.setattr(llm_gate, "DEFAULT_WAIT_SECONDS", 0.05)
        fake_redis.set("jobapp:llm:gate:freeinference", "busy")
        with patch("app.llm.providers._call_openai_compatible") as call:
            with pytest.raises(llm_gate.Busy):
                call_provider(self._provider(), [{"role": "user", "content": "hi"}])
        call.assert_not_called()

    def test_the_generation_chain_steps_over_a_busy_provider(
        self, fake_redis, monkeypatch
    ):
        from app.llm.providers import generation_chat

        monkeypatch.setattr(llm_gate, "_POLL_INTERVAL", 0.01)
        monkeypatch.setattr(llm_gate, "DEFAULT_WAIT_SECONDS", 0.05)
        fake_redis.set("jobapp:llm:gate:freeinference", "busy")
        with patch.multiple(
            "app.llm.providers.settings",
            FREEINFERENCE_API_KEY="fk", FREEINFERENCE_MODEL="glm-5.1",
            FREEINFERENCE_BASE_URL="https://fi.example/v1",
            FREEINFERENCE_MAX_CONCURRENCY=1,
            ANTHROPIC_API_KEY="", GEMINI_API_KEY="", create=True,
        ):
            with patch("app.llm.providers._call_openai_compatible",
                       return_value="from primary") as call:
                result = generation_chat(
                    [{"role": "user", "content": "hi"}], "nk", "http://nim", "llama"
                )
        assert result == "from primary"
        assert call.call_args[0][0].name == "primary"


class TestFreeInferenceProvider:
    def _settings(self, **overrides):
        base = dict(
            FREEINFERENCE_API_KEY="fk",
            FREEINFERENCE_MODEL="glm-5.1",
            FREEINFERENCE_MATCH_MODEL="glm-5-turbo",
            FREEINFERENCE_BASE_URL="https://fi.example/v1",
            FREEINFERENCE_MAX_CONCURRENCY=1,
            ANTHROPIC_API_KEY="", GEMINI_API_KEY="", create=True,
        )
        base.update(overrides)
        return patch.multiple("app.llm.providers.settings", **base)

    def test_a_key_is_all_it_takes(self):
        with self._settings():
            provider = configured_providers()["freeinference"]
        assert provider.base_url == "https://fi.example/v1"
        assert provider.max_concurrency == 1

    def test_it_is_not_billable(self):
        # Which is why it should not be spending the paid-call budget.
        with self._settings():
            assert configured_providers()["freeinference"].paid is False

    def test_matching_uses_the_faster_sibling_model(self):
        with self._settings():
            provider = matching_fallbacks()[0]
        assert provider.name == "freeinference"
        assert provider.model == "glm-5-turbo"

    def test_the_model_swap_does_not_lose_the_concurrency_limit(self):
        # Rebuilding the provider by hand instead of replacing one field is how
        # a limit like this gets silently dropped on the path that uses it most.
        with self._settings():
            assert matching_fallbacks()[0].max_concurrency == 1

    def test_free_is_tried_before_paid(self):
        with self._settings(ANTHROPIC_API_KEY="ak", GEMINI_API_KEY="gk"):
            assert [p.name for p in matching_fallbacks()][0] == "freeinference"

    def test_no_key_means_no_provider(self):
        with self._settings(FREEINFERENCE_API_KEY=""):
            assert "freeinference" not in configured_providers()


class TestPaidBudgetExemption:
    def _job(self):
        job = MagicMock()
        job.id = "job-1"
        return job

    def test_a_free_provider_does_not_spend_the_paid_budget(self):
        from app.services.matcher import _score_via_fallbacks

        free = Provider(name="freeinference", api_key="k", model="glm-5-turbo",
                        base_url="https://fi.example/v1", paid=False)
        budget = {"paid_calls": 0}
        with patch("app.services.matcher.matching_fallbacks", return_value=[free]):
            with patch("app.services.matcher.call_provider",
                       return_value='{"score": 80, "matched_skills": [], '
                                    '"missing_skills": [], "reasoning": "ok"}'):
                result = _score_via_fallbacks([], self._job(), budget)
        assert result is not None
        assert budget["paid_calls"] == 0

    def test_a_paid_provider_still_does(self):
        from app.services.matcher import _score_via_fallbacks

        paid = Provider(name="anthropic", api_key="k", model="claude-haiku-4-5")
        budget = {"paid_calls": 0}
        with patch("app.services.matcher.matching_fallbacks", return_value=[paid]):
            with patch("app.services.matcher.call_provider",
                       return_value='{"score": 80, "matched_skills": [], '
                                    '"missing_skills": [], "reasoning": "ok"}'):
                _score_via_fallbacks([], self._job(), budget)
        assert budget["paid_calls"] == 1

    def test_an_exhausted_budget_does_not_block_the_free_provider_behind_it(self):
        # Abandoning the whole chain at the cap would skip a provider that
        # cannot cost anything — the one most worth reaching at that point.
        from app.config import settings
        from app.services.matcher import _score_via_fallbacks

        paid = Provider(name="anthropic", api_key="k", model="claude-haiku-4-5")
        free = Provider(name="freeinference", api_key="k", model="glm-5-turbo",
                        base_url="https://fi.example/v1", paid=False)
        budget = {"paid_calls": settings.MAX_PAID_MATCH_CALLS_PER_CYCLE}
        with patch("app.services.matcher.matching_fallbacks", return_value=[paid, free]):
            with patch("app.services.matcher.call_provider",
                       return_value='{"score": 80, "matched_skills": [], '
                                    '"missing_skills": [], "reasoning": "ok"}') as call:
                result = _score_via_fallbacks([], self._job(), budget)
        assert result is not None
        assert call.call_args[0][0].name == "freeinference"


class TestProviderCheck:
    def test_it_reports_a_working_provider(self):
        from app.services import provider_check

        with patch("app.services.provider_check.configured_providers",
                   return_value={"freeinference": Provider(
                       name="freeinference", api_key="k", model="glm-5.1",
                       base_url="https://fi.example/v1", max_concurrency=1)}):
            with patch("app.services.provider_check.call_provider", return_value="ready"):
                with patch.object(provider_check.settings, "NVIDIA_NIM_API_KEY", ""):
                    results = provider_check.check_providers()
        assert results[0]["ok"] is True
        assert results[0]["gated"] is True

    def test_a_probe_does_not_wait_a_working_calls_timeout(self):
        # It is a reachability check. A provider that needs 90 seconds to say
        # one word is a finding, not something to sit through.
        from app.llm.providers import DEFAULT_TIMEOUT_SECONDS
        from app.services import provider_check

        with patch("app.services.provider_check.configured_providers",
                   return_value={"fi": Provider(name="fi", api_key="k", model="m")}):
            with patch("app.services.provider_check.call_provider",
                       return_value="ready") as call:
                with patch.object(provider_check.settings, "NVIDIA_NIM_API_KEY", ""):
                    provider_check.check_providers()
        assert call.call_args.kwargs["timeout"] < DEFAULT_TIMEOUT_SECONDS

    def test_a_probe_does_not_queue_politely_behind_real_work(self):
        # Whether a single-slot provider is busy right now is part of what the
        # check is reporting, so it should not wait two minutes to find out.
        from app.services import llm_gate, provider_check

        with patch("app.services.provider_check.configured_providers",
                   return_value={"fi": Provider(name="fi", api_key="k", model="m",
                                                max_concurrency=1)}):
            with patch("app.services.provider_check.call_provider",
                       return_value="ready") as call:
                with patch.object(provider_check.settings, "NVIDIA_NIM_API_KEY", ""):
                    provider_check.check_providers()
        assert call.call_args.kwargs["gate_wait"] < llm_gate.DEFAULT_WAIT_SECONDS

    def test_a_failure_names_the_reason(self):
        # "Nothing happened" is what this check exists to replace.
        from app.services import provider_check

        with patch("app.services.provider_check.configured_providers",
                   return_value={"freeinference": Provider(
                       name="freeinference", api_key="bad", model="glm-5.1")}):
            with patch("app.services.provider_check.call_provider",
                       side_effect=RuntimeError("401 invalid api key")):
                with patch.object(provider_check.settings, "NVIDIA_NIM_API_KEY", ""):
                    results = provider_check.check_providers()
        assert results[0]["ok"] is False
        assert "401" in results[0]["detail"]

    def test_an_empty_reply_is_a_result_not_a_crash(self):
        # A reasoning model can spend the whole token budget thinking.
        from app.services import provider_check

        with patch("app.services.provider_check.configured_providers",
                   return_value={"fi": Provider(name="fi", api_key="k", model="m")}):
            with patch("app.services.provider_check.call_provider", return_value=""):
                with patch.object(provider_check.settings, "NVIDIA_NIM_API_KEY", ""):
                    results = provider_check.check_providers()
        assert results[0]["ok"] is False
        assert "nothing" in results[0]["detail"]

    def test_the_primary_is_checked_too(self):
        from app.services import provider_check

        with patch("app.services.provider_check.configured_providers", return_value={}):
            with patch("app.services.provider_check.call_provider", return_value="ready"):
                with patch.object(provider_check.settings, "NVIDIA_NIM_API_KEY", "nk"):
                    results = provider_check.check_providers()
        assert [r["name"] for r in results] == ["nim"]


class TestTheCheckRunsOffTheRequest:
    """
    The proxy gives an upstream 60 seconds. Real calls to real providers, one of
    which may be queueing behind another caller, do not reliably fit in that —
    and an answer that arrives as a 504 is not an answer.
    """

    @pytest.fixture
    def profile(self, db):
        from app.models.profile import Profile

        record = Profile(data={})
        db.add(record)
        db.commit()
        return record

    def test_the_button_queues_instead_of_calling(self, client, profile):
        with patch("app.services.provider_check.check_providers") as check:
            with patch("app.tasks.providers.run_provider_check.delay") as delay:
                response = client.post("/runs/providers/check")
        assert response.status_code == 200
        assert delay.called
        check.assert_not_called(), "no provider call may happen in the request"

    def test_the_click_is_visible_before_a_worker_picks_it_up(
        self, client, db, profile
    ):
        # Otherwise "queued" and "never asked for" look identical and the
        # button appears to do nothing at all.
        from app.services import provider_check

        with patch("app.tasks.providers.run_provider_check.delay"):
            body = client.post("/runs/providers/check").text
        assert provider_check.load_state(db)["status"] == "queued"
        assert "Calling each one in turn" in body

    def test_a_broker_that_is_down_is_reported_not_swallowed(
        self, client, db, profile
    ):
        with patch("app.tasks.providers.run_provider_check.delay",
                   side_effect=RuntimeError("redis down")):
            response = client.post("/runs/providers/check")
        assert response.status_code == 200
        assert provider_check_status(db) == "failed"

    def test_the_task_stores_what_it_found(self, db, profile, monkeypatch):
        import app.tasks.providers as task_module
        from app.services import provider_check
        from app.tasks.providers import run_provider_check

        monkeypatch.setattr(task_module, "SessionLocal", lambda: db)
        monkeypatch.setattr(
            "app.services.provider_check.check_providers",
            lambda: [{"name": "freeinference", "model": "glm-5.1", "ok": True,
                      "ms": 812, "detail": "ready", "gated": True}],
        )
        run_provider_check.apply()
        record = provider_check.load_state(db)
        assert record["status"] == "done"
        assert record["results"][0]["name"] == "freeinference"

    def test_a_failure_is_stored_rather_than_left_spinning(
        self, db, profile, monkeypatch
    ):
        import app.tasks.providers as task_module
        from app.services import provider_check
        from app.tasks.providers import run_provider_check

        monkeypatch.setattr(task_module, "SessionLocal", lambda: db)
        monkeypatch.setattr(
            "app.services.provider_check.check_providers",
            lambda: (_ for _ in ()).throw(RuntimeError("openai package missing")),
        )
        run_provider_check.apply()
        record = provider_check.load_state(db)
        assert record["status"] == "failed"
        assert "openai" in record["error"]

    def test_a_check_whose_worker_died_is_called_stalled(self):
        from datetime import datetime, timedelta, timezone

        from app.services.provider_check import progress

        long_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        state = progress({"status": "running", "started_at": long_ago, "results": []})
        assert state["stalled"] is True
        assert state["active"] is False, "polling a dead check forever helps nobody"


def provider_check_status(db):
    from app.services import provider_check

    return (provider_check.load_state(db) or {}).get("status")


class TestProviderPanel:
    @pytest.fixture
    def profile(self, db):
        from app.models.profile import Profile

        record = Profile(data={})
        db.add(record)
        db.commit()
        return record

    def test_the_page_offers_the_check(self, client):
        assert "Test each provider" in client.get("/runs").text

    def test_stored_results_are_shown(self, client, db, profile):
        from app.services import provider_check

        provider_check.store_state(db, {
            "status": "done", "finished_at": provider_check._now(),
            "results": [{"name": "freeinference", "model": "glm-5.1", "ok": True,
                         "ms": 812, "detail": "ready", "gated": True}],
        })
        body = client.get("/runs/system").text
        assert "freeinference" in body
        assert "812" in body

    def test_it_polls_while_a_check_is_in_flight(self, client, db, profile):
        from app.services import provider_check

        provider_check.mark_queued(db)
        body = client.get("/runs/system").text
        assert 'hx-trigger="load delay:3s"' in body, "the panel must come back on its own"
        assert "Calling each one in turn" in body

    def test_it_does_not_poll_once_there_is_an_answer(self, client, db, profile):
        from app.services import provider_check

        provider_check.store_state(db, {
            "status": "done", "finished_at": provider_check._now(),
            "results": [{"name": "nim", "model": "m", "ok": True, "ms": 10,
                         "detail": "ready", "gated": False}],
        })
        body = client.get("/runs/system").text
        assert "hx-trigger=\"load delay:3s\"" not in body

    def test_a_stored_failure_is_readable(self, client, db, profile):
        from app.services import provider_check

        provider_check.store_state(db, {
            "status": "failed", "results": [],
            "error": "could not queue it: redis down",
        })
        assert "redis down" in client.get("/runs/system").text
