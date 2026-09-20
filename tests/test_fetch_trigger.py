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
        # Every key it took, not just one: a combined run holds the shared key
        # and all three group keys.
        released = {call.kwargs["key"] for call in release.call_args_list}
        assert released == set(fetch_task.ALL_LOCK_KEYS)


class TestFetchGroupLocks:
    """
    Groups touch disjoint sources, so they must not exclude each other.

    Every group run used to take the shared key as well as its own, which made
    the three-way split decorative: the hourly API tier skipped itself for the
    length of a twice-daily browser run.
    """

    def _keys_taken(self, group):
        import app.tasks.fetch as fetch_task
        taken = []

        def _acquire(key=None, **kwargs):
            taken.append(key)
            return True

        with patch.object(fetch_task, "acquire", side_effect=_acquire), \
             patch.object(fetch_task, "release"), \
             patch.object(fetch_task, "SessionLocal", return_value=MagicMock()), \
             patch.object(fetch_task, "fetch_and_save_jobs",
                          return_value={"fetched": 0, "inserted": 0,
                                        "merged": 0, "skipped": 0}):
            fetch_task.fetch_jobs.apply(
                kwargs={"group": group, "match_after": False})
        return taken

    def test_a_group_run_takes_only_its_own_key(self):
        import app.tasks.fetch as fetch_task
        assert self._keys_taken("api") == [fetch_task.GROUP_LOCK_KEYS["api"]]

    def test_a_group_run_does_not_take_the_shared_key(self):
        """The bug: this is what blocked every other group."""
        import app.tasks.fetch as fetch_task
        assert fetch_task.LOCK_KEY not in self._keys_taken("browser")

    def test_two_different_groups_do_not_contend(self):
        import app.tasks.fetch as fetch_task
        api = set(self._keys_taken("api"))
        browser = set(self._keys_taken("browser"))
        assert api and browser and not (api & browser)

    def test_a_combined_run_takes_every_group_key(self):
        """So "fetch everything" still cannot overlap a scheduled group."""
        import app.tasks.fetch as fetch_task
        taken = set(self._keys_taken(None))
        assert taken == set(fetch_task.ALL_LOCK_KEYS)
        for key in fetch_task.GROUP_LOCK_KEYS.values():
            assert key in taken

    def test_a_group_holding_its_key_turns_a_combined_run_away(self):
        import app.tasks.fetch as fetch_task
        busy = fetch_task.GROUP_LOCK_KEYS["boards"]

        def _acquire(key=None, **kwargs):
            return key != busy

        with patch.object(fetch_task, "acquire", side_effect=_acquire), \
             patch.object(fetch_task, "release") as release, \
             patch.object(fetch_task, "SessionLocal", return_value=MagicMock()), \
             patch.object(fetch_task, "fetch_and_save_jobs") as work:
            result = fetch_task.fetch_jobs.apply().result
        work.assert_not_called()
        assert result["skipped_reason"] == "already running"
        # And it gives back whatever it had already claimed, or the next run
        # would find keys held by a cycle that never started.
        assert release.call_args_list


class TestFetchStateAcrossGroups:
    def test_a_running_group_still_reads_as_running(self):
        """
        The runs page asks "is a fetch happening", not "is the shared key set".

        Once a group stopped taking the shared key, reading that key alone
        would have reported an idle system through an entire browser cycle.
        """
        import app.tasks.fetch as fetch_task

        busy = fetch_task.GROUP_LOCK_KEYS["browser"]
        client = MagicMock()
        client.exists.side_effect = lambda key: 1 if key == busy else 0
        client.ttl.return_value = 300
        with patch("app.services.fetch_lock._client", return_value=client):
            assert fetch_task.fetch_state() == {"running": True,
                                                "seconds_left": 300}

    def test_nothing_held_reads_as_idle(self):
        import app.tasks.fetch as fetch_task
        client = MagicMock()
        client.exists.return_value = 0
        with patch("app.services.fetch_lock._client", return_value=client):
            assert fetch_task.fetch_state()["running"] is False

    def test_the_longest_remaining_lease_is_the_one_reported(self):
        """Idle is when the slowest run finishes, not the first."""
        import app.tasks.fetch as fetch_task
        client = MagicMock()
        client.exists.return_value = 1
        client.ttl.side_effect = [30, 900, 120, 60]
        with patch("app.services.fetch_lock._client", return_value=client):
            assert fetch_task.fetch_state()["seconds_left"] == 900

    def test_a_broken_redis_reads_as_idle_rather_than_raising(self):
        import app.tasks.fetch as fetch_task
        with patch("app.services.fetch_lock._client", side_effect=RuntimeError("down")):
            state = fetch_task.fetch_state()
        assert state["running"] is False
        assert "down" in state["error"]

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


class TestGroupTriggers:
    """
    The three slices the schedule runs, startable by hand. Each has its own
    lock, so starting one does not block the others.
    """

    def _run(self, db, group="api"):
        from datetime import datetime, timezone
        from app.models.fetch_run import FetchRun

        db.add(FetchRun(started_at=datetime.now(timezone.utc), status="ok",
                        group=group, fetched=10, inserted=3))
        db.commit()

    def test_the_page_offers_a_button_per_group(self, client, db):
        body = client.get("/runs").text
        for group in ("api", "boards", "browser"):
            assert f'name="group" value="{group}"' in body, group

    def test_a_run_is_labelled_with_its_group(self, client, db):
        self._run(db, group="boards")
        body = client.get("/runs").text
        assert "Group" in body
        assert "boards" in body

    def test_triggering_a_group_queues_it(self, client, db):
        from unittest.mock import patch

        with patch("app.tasks.fetch.fetch_jobs.delay") as delay, \
             patch("app.tasks.fetch.fetch_state",
                   return_value={"running": False, "seconds_left": None}):
            response = client.post("/runs/trigger", data={"group": "api"})

        assert response.status_code == 200
        assert delay.call_args.kwargs["group"] == "api"
        # A whole group is what the schedule runs, not a narrow adapter test,
        # so it keeps its tail-call to matching.
        assert delay.call_args.kwargs["match_after"] is True

    def test_an_unknown_group_is_ignored_rather_than_fetching_nothing(self, client, db):
        from unittest.mock import patch

        with patch("app.tasks.fetch.fetch_jobs.delay") as delay, \
             patch("app.tasks.fetch.fetch_state",
                   return_value={"running": False, "seconds_left": None}):
            client.post("/runs/trigger", data={"group": "nonsense"})

        assert delay.call_args.kwargs["group"] is None
