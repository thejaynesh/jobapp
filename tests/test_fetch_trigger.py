from unittest.mock import MagicMock, patch

import pytest

from app.services import fetch_lock


def _redis(exists=0, ttl=120, set_result=True) -> MagicMock:
    client = MagicMock()
    client.set.return_value = set_result
    client.exists.return_value = exists
    client.ttl.return_value = ttl
    return client


class TestFetchLock:
    def test_acquire_uses_set_nx_with_expiry(self):
        """NX makes it a lock; EX stops a killed worker wedging fetching."""
        client = _redis()
        with patch("app.services.fetch_lock._client", return_value=client):
            assert fetch_lock.acquire(ttl=99) is True
        kwargs = client.set.call_args.kwargs
        assert kwargs["nx"] is True
        assert kwargs["ex"] == 99

    def test_acquire_is_false_when_already_held(self):
        with patch("app.services.fetch_lock._client", return_value=_redis(set_result=None)):
            assert fetch_lock.acquire() is False

    def test_a_broken_redis_does_not_block_fetching(self):
        """Refusing to work because the lock service is down is worse."""
        with patch("app.services.fetch_lock._client", side_effect=RuntimeError("down")):
            assert fetch_lock.acquire() is True

    def test_release_deletes_only_its_own_key(self):
        # Compare-and-delete, not a blind DELETE: a cycle that outlived its TTL
        # must not delete the lock its successor is now holding.
        client = _redis()
        with patch("app.services.fetch_lock._client", return_value=client):
            assert fetch_lock.acquire() is True
            fetch_lock.release()
        client.delete.assert_not_called()
        script, numkeys, key, token = client.eval.call_args.args
        assert numkeys == 1
        assert key == fetch_lock.LOCK_KEY
        assert token == client.set.call_args.args[1]

    def test_release_without_holding_touches_nothing(self):
        fetch_lock._held_tokens.clear()
        client = _redis()
        with patch("app.services.fetch_lock._client", return_value=client):
            fetch_lock.release()
        client.delete.assert_not_called()
        client.eval.assert_not_called()

    def test_release_survives_a_broken_redis(self):
        fetch_lock._held_tokens[fetch_lock.LOCK_KEY] = "tok"
        with patch("app.services.fetch_lock._client", side_effect=RuntimeError("down")):
            fetch_lock.release()  # must not raise

    def test_state_reports_running_with_time_left(self):
        with patch("app.services.fetch_lock._client", return_value=_redis(exists=1, ttl=300)):
            assert fetch_lock.state() == {"running": True, "seconds_left": 300}

    def test_state_reports_idle(self):
        with patch("app.services.fetch_lock._client", return_value=_redis(exists=0)):
            assert fetch_lock.state() == {"running": False, "seconds_left": None}

    def test_state_surfaces_a_redis_problem_without_raising(self):
        with patch("app.services.fetch_lock._client", side_effect=RuntimeError("down")):
            result = fetch_lock.state()
        assert result["running"] is False
        assert "down" in result["error"]


class TestFetchTaskLocking:
    def test_the_task_refuses_to_overlap_a_running_fetch(self):
        import app.tasks.fetch as fetch_task
        with patch.object(fetch_task, "acquire", return_value=False):
            with patch.object(fetch_task, "fetch_and_save_jobs") as work:
                result = fetch_task.fetch_jobs.apply().result
        work.assert_not_called()
        assert result["skipped_reason"] == "already running"

    def test_the_lock_is_released_even_when_the_cycle_raises(self):
        import app.tasks.fetch as fetch_task
        with patch.object(fetch_task, "acquire", return_value=True), \
             patch.object(fetch_task, "release") as release, \
             patch.object(fetch_task, "SessionLocal", return_value=MagicMock()), \
             patch.object(fetch_task, "fetch_and_save_jobs",
                          side_effect=RuntimeError("boom")):
            fetch_task.fetch_jobs.apply()
        release.assert_called_once()

    def test_selected_sources_are_passed_through(self):
        import app.tasks.fetch as fetch_task
        with patch.object(fetch_task, "acquire", return_value=True), \
             patch.object(fetch_task, "release"), \
             patch.object(fetch_task, "SessionLocal", return_value=MagicMock()), \
             patch.object(fetch_task, "fetch_and_save_jobs",
                          return_value={"fetched": 0, "inserted": 0, "merged": 0,
                                        "skipped": 0}) as work:
            fetch_task.fetch_jobs.apply(kwargs={"only": ["arbeitnow"],
                                                "match_after": False})
        assert work.call_args.kwargs["only"] == {"arbeitnow"}

    def test_matching_is_skipped_when_asked(self):
        import app.tasks.fetch as fetch_task
        with patch.object(fetch_task, "acquire", return_value=True), \
             patch.object(fetch_task, "release"), \
             patch.object(fetch_task, "SessionLocal", return_value=MagicMock()), \
             patch.object(fetch_task, "fetch_and_save_jobs",
                          return_value={"fetched": 0, "inserted": 0, "merged": 0,
                                        "skipped": 0}), \
             patch("app.tasks.match.match_jobs") as match:
            fetch_task.fetch_jobs.apply(kwargs={"match_after": False})
        match.delay.assert_not_called()


class TestSourceFiltering:
    """`only` is what makes a manual test run fast."""

    def _cfg(self):
        cfg = MagicMock()
        cfg.ADZUNA_APP_ID = ""
        cfg.ADZUNA_APP_KEY = ""
        cfg.JSEARCH_API_KEY = ""
        cfg.JOOBLE_API_KEY = ""
        cfg.CAREERJET_AFFID = ""
        cfg.FINDWORK_API_KEY = ""
        cfg.LINKEDIN_SESSION_COOKIE = ""
        cfg.HANDSHAKE_SESSION_COOKIE = ""
        cfg.INDEED_RSS_ENABLED = False
        cfg.ARBEITNOW_MAX_PAGES = 1
        return cfg

    def test_only_the_requested_source_is_called(self):
        from app.services.job_fetcher import _run_all_adapters
        with patch("app.services.sources.arbeitnow.fetch",
                   return_value=[{"source": "arbeitnow", "title": "SWE"}]) as arb, \
             patch("app.services.sources.remotive.fetch") as remotive, \
             patch("app.services.sources.hnhiring.fetch") as hn:
            jobs, stats = _run_all_adapters(
                ["SWE"], ["Remote"], self._cfg(), {}, {}, only={"arbeitnow"},
            )
        assert arb.called
        remotive.assert_not_called()
        hn.assert_not_called()
        assert len(jobs) == 1

    def test_skipped_sources_are_reported_as_disabled_not_missing(self):
        from app.services.job_fetcher import _run_all_adapters
        with patch("app.services.sources.arbeitnow.fetch", return_value=[]):
            _, stats = _run_all_adapters(
                ["SWE"], ["Remote"], self._cfg(), {}, {}, only={"arbeitnow"},
            )
        assert stats["remotive"]["enabled"] is False
        assert stats["hnhiring"]["enabled"] is False
        assert stats["arbeitnow"]["enabled"] is True

    def test_the_browser_tier_is_not_launched_when_unwanted(self):
        """Starting Chromium is the single most expensive step."""
        from app.services.job_fetcher import _run_all_adapters
        with patch("app.services.sources.arbeitnow.fetch", return_value=[]), \
             patch("asyncio.run") as async_run:
            _, stats = _run_all_adapters(
                ["SWE"], ["Remote"], self._cfg(), {}, {}, only={"arbeitnow"},
            )
        async_run.assert_not_called()
        assert stats["wellfound"]["enabled"] is False
        assert stats["dice"]["enabled"] is False

    def test_the_browser_tier_runs_when_one_of_its_sources_is_asked_for(self):
        from app.services.job_fetcher import _run_all_adapters
        with patch("asyncio.run", return_value=([], {})) as async_run:
            _run_all_adapters(
                ["SWE"], ["Remote"], self._cfg(), {}, {}, only={"dice"},
            )
        async_run.assert_called_once()

    def test_no_filter_runs_everything(self):
        from app.services.job_fetcher import _run_all_adapters
        with patch("app.services.sources.arbeitnow.fetch", return_value=[]), \
             patch("app.services.sources.remotive.fetch", return_value=[]) as remotive, \
             patch("app.services.sources.remoteok.fetch", return_value=[]), \
             patch("app.services.sources.weworkremotely.fetch", return_value=[]), \
             patch("app.services.sources.themuse.fetch", return_value=[]), \
             patch("app.services.sources.himalayas.fetch", return_value=[]), \
             patch("app.services.sources.jobicy.fetch", return_value=[]), \
             patch("app.services.sources.hnhiring.fetch", return_value=[]), \
             patch("app.services.sources.linkedin.fetch_all", return_value=[]), \
             patch("asyncio.run", return_value=([], {})):
            _run_all_adapters(["SWE"], ["Remote"], self._cfg(), {}, {})
        assert remotive.called

    def test_an_ats_board_can_be_targeted_alone(self):
        from app.services.job_fetcher import _run_all_adapters
        with patch("app.services.sources.greenhouse.fetch", return_value=[]) as gh, \
             patch("app.services.sources.lever.fetch") as lever:
            _run_all_adapters(
                ["SWE"], ["Remote"], self._cfg(),
                {"greenhouse": ["stripe"], "lever": ["netflix"]}, {},
                only={"greenhouse"},
            )
        assert gh.called
        lever.assert_not_called()
