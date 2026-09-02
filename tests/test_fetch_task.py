from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import pytest

from app.models.job import Job, JobStatus
from app.models.profile import Profile
from app.services.profile_service import get_or_create_profile, save_section

_NOW = datetime.now(timezone.utc)


def _make_profile_with_targets(db):
    get_or_create_profile(db)
    profile = db.query(Profile).first()
    import copy
    data = copy.deepcopy(profile.data)
    data["target_roles"] = ["Software Engineer"]
    data["target_locations"] = ["New York, NY"]
    profile.data = data
    db.flush()
    return profile


def _std_job(*, title="SWE", company="ACME", location="NYC",
             url="https://ex.com/1", source_job_id="J1",
             description="Build things.", source="adzuna", **extra) -> dict:
    """
    One adapter's take on a posting.

    `**extra` is how a test says "this source stated something the last one
    didn't" — a salary band, an employment type, a posting date — which is the
    whole point of the sources being different.
    """
    return {
        "source": source,
        "source_job_id": source_job_id,
        "title": title,
        "company": company,
        "location": location,
        "is_remote": False,
        "url": url,
        "description": description,
        "experience_level": "mid",
        **extra,
    }


# ---------------------------------------------------------------------------
# Orchestrator tests
# ---------------------------------------------------------------------------

def _patch_adapters(jobs=None, side_effect=None, stats=None):
    """
    Patch the adapter runner (returns (jobs, stats)) and skip LLM query expansion.

    `stats` defaults to marking every source of the supplied jobs as having
    run. The real runner always records an entry per source, and board yield is
    only counted for ATSes a run actually polled — so an empty stats dict here
    would mean "this run touched nothing", which is not what these tests are
    describing.
    """
    from contextlib import ExitStack
    stack = ExitStack()
    stack.enter_context(patch(
        "app.services.query_expansion.expand_search_queries",
        return_value=(["Software Engineer"], None),
    ))
    if side_effect is not None:
        stack.enter_context(patch(
            "app.services.job_fetcher._run_all_adapters", side_effect=side_effect))
    else:
        jobs = jobs or []
        if stats is None:
            stats = {
                job.get("source", ""): {"count": 0, "errors": [], "enabled": True}
                for job in jobs if job.get("source")
            }
        stack.enter_context(patch(
            "app.services.job_fetcher._run_all_adapters", return_value=(jobs, stats)))
    return stack


class TestFetchAndSaveJobs:
    def test_inserts_new_job(self, db):
        from app.services.job_fetcher import fetch_and_save_jobs
        _make_profile_with_targets(db)
        with _patch_adapters([_std_job()]):
            result = fetch_and_save_jobs(db)
        assert result["inserted"] == 1
        assert result["fetched"] == 1
        saved = db.query(Job).first()
        assert saved is not None
        assert saved.status == JobStatus.new

    def test_skips_duplicate_url(self, db):
        from app.services.job_fetcher import fetch_and_save_jobs
        _make_profile_with_targets(db)
        jobs = [_std_job(url="https://ex.com/dup", source_job_id="DUP1")]
        with _patch_adapters(jobs):
            r1 = fetch_and_save_jobs(db)
        with _patch_adapters(jobs):
            r2 = fetch_and_save_jobs(db)
        assert r1["inserted"] == 1
        assert r2["skipped"] == 1
        assert db.query(Job).count() == 1

    def test_merges_cross_posted_job(self, db):
        from app.services.job_fetcher import fetch_and_save_jobs
        _make_profile_with_targets(db)
        j1 = _std_job(url="https://adzuna.com/1", source_job_id="AZ1",
                      title="SWE", company="ACME", location="NYC")
        j2 = _std_job(url="https://indeed.com/1", source_job_id=None,
                      title="SWE", company="ACME", location="NYC", source="indeed")
        with _patch_adapters([j1]):
            fetch_and_save_jobs(db)
        with _patch_adapters([j2]):
            r2 = fetch_and_save_jobs(db)
        assert r2["merged"] == 1
        job = db.query(Job).first()
        assert "https://indeed.com/1" in job.source_urls

    def test_no_profile_returns_zeros(self, db):
        from app.services.job_fetcher import fetch_and_save_jobs
        result = fetch_and_save_jobs(db)
        assert result["fetched"] == 0
        assert result["inserted"] == 0

    def test_empty_target_roles_returns_zeros(self, db):
        from app.services.job_fetcher import fetch_and_save_jobs
        get_or_create_profile(db)
        db.flush()
        result = fetch_and_save_jobs(db)
        assert result["fetched"] == 0
        assert result["inserted"] == 0

    def test_adapter_error_does_not_crash(self, db):
        from app.services.job_fetcher import fetch_and_save_jobs
        _make_profile_with_targets(db)
        with _patch_adapters(side_effect=RuntimeError("adapter exploded")):
            result = fetch_and_save_jobs(db)
        assert result["fetched"] == 0

    def test_multiple_jobs_counted_correctly(self, db):
        from app.services.job_fetcher import fetch_and_save_jobs
        _make_profile_with_targets(db)
        jobs = [
            _std_job(title="SWE", company="Alpha", url="https://ex.com/a", source_job_id="A1"),
            _std_job(title="SRE", company="Beta", url="https://ex.com/b", source_job_id="B1"),
            _std_job(title="DevOps", company="Gamma", url="https://ex.com/c", source_job_id="C1"),
        ]
        with _patch_adapters(jobs):
            result = fetch_and_save_jobs(db)
        assert result["inserted"] == 3
        assert result["merged"] == 0
        assert result["skipped"] == 0

    def test_expanded_queries_passed_to_adapters(self, db):
        from app.services.job_fetcher import fetch_and_save_jobs
        _make_profile_with_targets(db)
        expanded = ["Software Engineer", "Java Developer"]
        with patch("app.services.query_expansion.expand_search_queries",
                   return_value=(expanded, {"basis": "h", "queries": expanded})):
            with patch("app.services.job_fetcher._run_all_adapters",
                       return_value=([], {})) as mock_run:
                fetch_and_save_jobs(db)
        assert mock_run.call_args[0][0] == expanded
        profile = db.query(Profile).first()
        assert profile.data["search_query_cache"]["queries"] == expanded

    def test_query_expansion_crash_falls_back_to_roles(self, db):
        from app.services.job_fetcher import fetch_and_save_jobs
        _make_profile_with_targets(db)
        with patch("app.services.query_expansion.expand_search_queries",
                   side_effect=RuntimeError("boom")):
            with patch("app.services.job_fetcher._run_all_adapters",
                       return_value=([], {})) as mock_run:
                fetch_and_save_jobs(db)
        assert mock_run.call_args[0][0] == ["Software Engineer"]


# ---------------------------------------------------------------------------
# Celery task tests
# ---------------------------------------------------------------------------

class TestFetchJobsTask:
    def test_task_is_registered(self):
        from app.celery_app import celery_app
        import app.tasks.fetch  # noqa — register task
        assert "app.tasks.fetch.fetch_jobs" in celery_app.tasks

    def test_task_calls_fetch_and_save_jobs(self):
        import app.tasks.fetch  # noqa
        from app.tasks.fetch import fetch_jobs

        mock_result = {"fetched": 5, "inserted": 3, "merged": 1, "skipped": 1}

        with patch("app.tasks.fetch.fetch_and_save_jobs", return_value=mock_result):
            with patch("app.tasks.fetch.SessionLocal") as mock_session_cls:
                with patch("app.tasks.match.match_jobs"):  # chained task needs no broker
                    mock_db = MagicMock()
                    mock_session_cls.return_value = mock_db
                    result = fetch_jobs.apply().result

        assert result == mock_result
        mock_db.close.assert_called_once()

    def test_task_closes_db_on_exception(self):
        import app.tasks.fetch  # noqa
        from app.tasks.fetch import fetch_jobs

        with patch("app.tasks.fetch.fetch_and_save_jobs", side_effect=RuntimeError("DB down")):
            with patch("app.tasks.fetch.SessionLocal") as mock_session_cls:
                mock_db = MagicMock()
                mock_session_cls.return_value = mock_db
                result = fetch_jobs.apply().result

        mock_db.close.assert_called_once()
        assert result == {"fetched": 0, "inserted": 0, "merged": 0, "skipped": 0}

    def test_beat_schedule_configured(self):
        """
        The schedule runs the three groups, not one task for everything —
        that is the whole point of the split, and scheduling the combined task
        as well would fetch everything twice.
        """
        from app.celery_app import celery_app
        schedule = celery_app.conf.beat_schedule
        for key, task in (
            ("fetch-api-sources", "app.tasks.fetch.fetch_api_sources"),
            ("fetch-ats-boards", "app.tasks.fetch.fetch_ats_boards"),
            ("fetch-browser-tier", "app.tasks.fetch.fetch_browser_tier"),
        ):
            assert key in schedule, key
            assert schedule[key]["task"] == task

    def test_beat_schedule_interval_matches_config(self):
        from app.celery_app import celery_app
        from app.config import settings
        schedule = celery_app.conf.beat_schedule
        for key, hours in (
            ("fetch-api-sources", settings.FETCH_API_INTERVAL_HOURS),
            ("fetch-ats-boards", settings.FETCH_BOARDS_INTERVAL_HOURS),
            ("fetch-browser-tier", settings.FETCH_BROWSER_INTERVAL_HOURS),
        ):
            assert schedule[key]["schedule"].seconds == hours * 3600, key

    def test_the_cheap_tier_refreshes_more_often_than_the_expensive_one(self):
        # If they were equal the split would have bought nothing.
        from app.config import settings

        assert (settings.FETCH_API_INTERVAL_HOURS
                < settings.FETCH_BROWSER_INTERVAL_HOURS)


class TestStaleJobFilter:
    def test_skips_job_older_than_max_age(self, db):
        from datetime import timedelta
        from app.services.job_fetcher import fetch_and_save_jobs
        _make_profile_with_targets(db)
        old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        job = _std_job()
        job["posted_at"] = old
        with _patch_adapters([job]):
            result = fetch_and_save_jobs(db)
        assert result["stale"] == 1
        assert result["inserted"] == 0

    def test_keeps_recent_job(self, db):
        from datetime import timedelta
        from app.services.job_fetcher import fetch_and_save_jobs
        _make_profile_with_targets(db)
        job = _std_job()
        job["posted_at"] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        with _patch_adapters([job]):
            result = fetch_and_save_jobs(db)
        assert result["inserted"] == 1
        assert result["stale"] == 0

    def test_keeps_job_without_posted_at(self, db):
        from app.services.job_fetcher import fetch_and_save_jobs
        _make_profile_with_targets(db)
        with _patch_adapters([_std_job()]):  # _std_job has no posted_at
            result = fetch_and_save_jobs(db)
        assert result["inserted"] == 1

    def test_naive_timestamp_treated_as_utc(self, db):
        from datetime import timedelta
        from app.services.job_fetcher import fetch_and_save_jobs
        _make_profile_with_targets(db)
        job = _std_job()
        # naive ISO string (no tz) from a sloppy source — must not crash
        job["posted_at"] = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%S")
        with _patch_adapters([job]):
            result = fetch_and_save_jobs(db)
        assert result["stale"] == 1


class TestAtsDiscoveryWiring:
    def test_persists_discovered_slugs_from_fetched_jobs(self, db):
        from app.services.job_fetcher import fetch_and_save_jobs
        _make_profile_with_targets(db)
        job = _std_job()
        job["description"] = "Apply: https://boards.greenhouse.io/coolstartup/jobs/1"
        with _patch_adapters([job]):
            fetch_and_save_jobs(db)
        profile = db.query(Profile).first()
        assert "coolstartup" in profile.data["discovered_ats"]["greenhouse"]

    def test_passes_existing_discovered_slugs_to_adapters(self, db):
        from app.services.job_fetcher import fetch_and_save_jobs
        profile = _make_profile_with_targets(db)
        import copy
        data = copy.deepcopy(profile.data)
        data["discovered_ats"] = {"lever": ["netflix"]}
        profile.data = data
        db.flush()
        with patch("app.services.query_expansion.expand_search_queries",
                   return_value=(["Software Engineer"], None)):
            with patch("app.services.job_fetcher._run_all_adapters",
                       return_value=([], {})) as mock_run:
                fetch_and_save_jobs(db)
        # 4th arg is the assembled slug map: configured + seeds + discovered
        slug_map = mock_run.call_args[0][3]
        assert "netflix" in slug_map["lever"]


class TestRunHistoryWiring:
    def test_a_cycle_records_a_run_with_per_source_detail(self, db):
        from app.models.fetch_run import FetchRun
        from app.services.job_fetcher import fetch_and_save_jobs
        _make_profile_with_targets(db)

        def _adapters(*args, **kwargs):
            return (
                [_std_job(source="linkedin", url="https://ex.com/li", source_job_id="L1")],
                {"linkedin": {"count": 1, "errors": [], "enabled": True},
                 "indeed": {"count": 0, "errors": [], "enabled": True}},
            )

        with patch("app.services.query_expansion.expand_search_queries",
                   return_value=(["Software Engineer"], None)):
            with patch("app.services.job_fetcher._run_all_adapters",
                       side_effect=_adapters):
                fetch_and_save_jobs(db)

        run = db.query(FetchRun).one()
        assert run.fetched == 1
        assert run.inserted == 1
        assert run.duration_seconds is not None
        assert run.queries == ["Software Engineer"]

        by_source = {s.source: s for s in run.sources}
        assert by_source["linkedin"].fetched == 1
        assert by_source["linkedin"].inserted == 1
        assert by_source["indeed"].status == "empty"

    def test_duplicates_are_attributed_to_the_source_that_refetched_them(self, db):
        """The number that tells you a source has stopped being useful."""
        from app.models.fetch_run import FetchRun
        from app.services.job_fetcher import fetch_and_save_jobs
        _make_profile_with_targets(db)
        job = _std_job(source="remotive", url="https://ex.com/dup", source_job_id="D1")

        def _adapters(*args, **kwargs):
            return ([dict(job)],
                    {"remotive": {"count": 1, "errors": [], "enabled": True}})

        with patch("app.services.query_expansion.expand_search_queries",
                   return_value=(["Software Engineer"], None)):
            with patch("app.services.job_fetcher._run_all_adapters",
                       side_effect=_adapters):
                fetch_and_save_jobs(db)   # first cycle inserts it
                fetch_and_save_jobs(db)   # second re-fetches the same posting

        latest = db.query(FetchRun).order_by(FetchRun.started_at.desc()).first()
        remotive = next(s for s in latest.sources if s.source == "remotive")
        assert remotive.fetched == 1
        assert remotive.inserted == 0
        assert remotive.skipped == 1

    def test_a_failing_source_marks_the_run_partial(self, db):
        import logging
        from app.models.fetch_run import FetchRun
        from app.services.job_fetcher import fetch_and_save_jobs
        _make_profile_with_targets(db)

        def _adapters(*args, **kwargs):
            logging.getLogger("app.services.sources.dice").error("Dice: blocked")
            return [], {"dice": {"count": 0, "errors": [], "enabled": True}}

        with patch("app.services.query_expansion.expand_search_queries",
                   return_value=(["Software Engineer"], None)):
            with patch("app.services.job_fetcher._run_all_adapters",
                       side_effect=_adapters):
                fetch_and_save_jobs(db)

        run = db.query(FetchRun).one()
        assert run.status == "partial"
        assert next(s for s in run.sources if s.source == "dice").status == "failed"

    def test_history_failure_does_not_lose_the_fetched_jobs(self, db):
        from app.services.job_fetcher import fetch_and_save_jobs
        _make_profile_with_targets(db)
        with patch("app.services.fetch_history.record_run",
                   side_effect=RuntimeError("history table missing")):
            with _patch_adapters([_std_job()]):
                result = fetch_and_save_jobs(db)
        assert result["inserted"] == 1
        assert db.query(Job).count() == 1


class TestSourceFailureVisibility:
    """A source that returned nothing must say why, not read as healthy."""

    def test_a_swallowed_adapter_error_reaches_the_stats(self, db):
        import logging
        from app.services.job_fetcher import fetch_and_save_jobs
        _make_profile_with_targets(db)

        def _adapters(*args, **kwargs):
            # Exactly what the real adapters do: log the reason, return [].
            logging.getLogger("app.services.sources.indeed").error(
                "Indeed RSS fetch error: 403 Forbidden"
            )
            return [], {"indeed": {"count": 0, "errors": [], "enabled": True}}

        with patch("app.services.query_expansion.expand_search_queries",
                   return_value=(["Software Engineer"], None)):
            with patch("app.services.job_fetcher._run_all_adapters",
                       side_effect=_adapters):
                result = fetch_and_save_jobs(db)

        assert result["sources"]["indeed"]["errors"] == [
            "Indeed RSS fetch error: 403 Forbidden"
        ]

    def test_the_reason_is_persisted_for_the_settings_page(self, db):
        import logging
        from app.services.job_fetcher import fetch_and_save_jobs
        _make_profile_with_targets(db)

        def _adapters(*args, **kwargs):
            logging.getLogger("app.services.sources.dice").warning(
                "Dice: page load failed: Timeout 30000ms exceeded"
            )
            return [], {"dice": {"count": 0, "errors": [], "enabled": True}}

        with patch("app.services.query_expansion.expand_search_queries",
                   return_value=(["Software Engineer"], None)):
            with patch("app.services.job_fetcher._run_all_adapters",
                       side_effect=_adapters):
                fetch_and_save_jobs(db)

        stored = db.query(Profile).first().data["last_fetch"]["sources"]["dice"]
        assert "Timeout" in stored["errors"][0]

    def test_a_working_source_is_not_polluted_by_its_warnings(self, db):
        import logging
        from app.services.job_fetcher import fetch_and_save_jobs
        _make_profile_with_targets(db)

        def _adapters(*args, **kwargs):
            logging.getLogger("app.services.sources.linkedin").warning(
                "LinkedIn throttled (1/3)"
            )
            return ([_std_job(source="linkedin")],
                    {"linkedin": {"count": 1, "errors": [], "enabled": True}})

        with patch("app.services.query_expansion.expand_search_queries",
                   return_value=(["Software Engineer"], None)):
            with patch("app.services.job_fetcher._run_all_adapters",
                       side_effect=_adapters):
                result = fetch_and_save_jobs(db)

        assert result["sources"]["linkedin"]["errors"] == []


class TestApplyLinkWiring:
    def _adzuna_job(self):
        return _std_job(url="https://www.adzuna.com/land/ad/9", source_job_id="AZ9")

    def test_resolved_apply_url_is_stored_on_the_job(self, db):
        from app.services.job_fetcher import fetch_and_save_jobs
        _make_profile_with_targets(db)

        def _resolve(jobs, **kwargs):
            from app.services.link_resolver import ResolveStats
            jobs[0]["apply_url"] = "https://boards.greenhouse.io/coolco/jobs/5"
            return ResolveStats(attempted=1, resolved=1)

        with patch("app.services.link_resolver.resolve_jobs", side_effect=_resolve):
            with _patch_adapters([self._adzuna_job()]):
                fetch_and_save_jobs(db)

        assert db.query(Job).one().apply_url == "https://boards.greenhouse.io/coolco/jobs/5"

    def test_the_ats_behind_the_redirect_becomes_a_known_board(self, db):
        from app.models.company_board import CompanyBoard
        from app.services.job_fetcher import fetch_and_save_jobs
        _make_profile_with_targets(db)

        def _resolve(jobs, **kwargs):
            from app.services.link_resolver import ResolveStats
            jobs[0]["apply_url"] = "https://jobs.lever.co/hiddenco/abc"
            return ResolveStats(attempted=1, resolved=1)

        with patch("app.services.link_resolver.resolve_jobs", side_effect=_resolve):
            with _patch_adapters([self._adzuna_job()]):
                fetch_and_save_jobs(db)

        board = db.query(CompanyBoard).filter(
            CompanyBoard.ats == "lever", CompanyBoard.slug == "hiddenco"
        ).one()
        assert board.origin == "discovered"

    def test_already_stored_urls_are_not_resolved_again(self, db):
        from app.services.job_fetcher import fetch_and_save_jobs
        _make_profile_with_targets(db)
        job = self._adzuna_job()
        with patch("app.services.link_resolver.resolve_jobs",
                   return_value=None) as resolve:
            with _patch_adapters([job]):
                fetch_and_save_jobs(db)
            assert resolve.call_count == 1
            with _patch_adapters([dict(job)]):
                fetch_and_save_jobs(db)
        # Second cycle saw the same URL already in the DB, so it skipped it.
        assert resolve.call_count == 1

    def test_resolution_failure_does_not_block_the_fetch(self, db):
        from app.services.job_fetcher import fetch_and_save_jobs
        _make_profile_with_targets(db)
        with patch("app.services.link_resolver.resolve_jobs",
                   side_effect=RuntimeError("proxy down")):
            with _patch_adapters([self._adzuna_job()]):
                result = fetch_and_save_jobs(db)
        assert result["inserted"] == 1

    def test_disabled_by_config(self, db):
        from app.config import settings
        from app.services.job_fetcher import fetch_and_save_jobs
        _make_profile_with_targets(db)
        with patch.object(settings, "RESOLVE_APPLY_LINKS", False):
            with patch("app.services.link_resolver.resolve_jobs") as resolve:
                with _patch_adapters([self._adzuna_job()]):
                    fetch_and_save_jobs(db)
        resolve.assert_not_called()


class TestBoardRegistryWiring:
    def test_boards_seen_in_job_links_are_registered(self, db):
        from app.models.company_board import CompanyBoard
        from app.services.job_fetcher import fetch_and_save_jobs
        _make_profile_with_targets(db)
        job = _std_job(description="Apply at https://boards.greenhouse.io/newco/jobs/1")
        with _patch_adapters([job]):
            fetch_and_save_jobs(db)
        assert db.query(CompanyBoard).filter(
            CompanyBoard.ats == "greenhouse", CompanyBoard.slug == "newco"
        ).count() == 1

    def test_registry_boards_are_polled_next_cycle(self, db):
        """The registry, not the legacy JSON blob, is what carries a board forward."""
        import copy
        from app.services.job_fetcher import fetch_and_save_jobs
        profile = _make_profile_with_targets(db)
        job = _std_job(description="https://jobs.ashbyhq.com/registryco/x")
        with _patch_adapters([job]):
            fetch_and_save_jobs(db)

        data = copy.deepcopy(profile.data)
        data["discovered_ats"] = {}
        profile.data = data
        db.flush()

        with patch("app.services.query_expansion.expand_search_queries",
                   return_value=(["Software Engineer"], None)):
            with patch("app.services.job_fetcher._run_all_adapters",
                       return_value=([], {})) as mock_run:
                fetch_and_save_jobs(db)
        assert "registryco" in mock_run.call_args[0][3]["ashby"]

    def test_per_board_yield_is_recorded(self, db):
        from app.models.company_board import CompanyBoard
        from app.services.company_boards import record_boards
        from app.services.job_fetcher import fetch_and_save_jobs
        _make_profile_with_targets(db)
        record_boards(db, {"greenhouse": ["busyco", "quietco"]}, origin="discovered")
        db.flush()

        gh_job = _std_job(source="greenhouse", url="https://boards.greenhouse.io/busyco/jobs/1",
                          source_job_id="GH1", company="BusyCo")
        gh_job["ats_slug"] = "busyco"
        with patch("app.services.ats_discovery.build_ats_slugs",
                   return_value={"greenhouse": ["busyco", "quietco"]}):
            with _patch_adapters([gh_job]):
                fetch_and_save_jobs(db)

        boards = {b.slug: b for b in db.query(CompanyBoard).all()}
        assert boards["busyco"].last_job_count == 1
        assert boards["busyco"].total_job_count == 1
        assert boards["quietco"].consecutive_empty == 1

    def test_careers_site_sniffing_registers_the_hidden_board(self, db):
        from app.models.company_board import CompanyBoard
        from app.services.job_fetcher import fetch_and_save_jobs
        _make_profile_with_targets(db)

        def _resolve(jobs, **kwargs):
            from app.services.link_resolver import ResolveStats
            jobs[0]["apply_url"] = "https://careers.sniffme.com/job/1"
            return ResolveStats(
                attempted=1, resolved=1,
                landing_html={jobs[0]["url"]:
                              '<iframe src="https://boards.greenhouse.io/embed/job_board?for=sniffme">'},
            )

        job = _std_job(url="https://www.adzuna.com/land/ad/77", source_job_id="AZ77")
        with patch("app.services.link_resolver.resolve_jobs", side_effect=_resolve):
            with _patch_adapters([job]):
                fetch_and_save_jobs(db)

        board = db.query(CompanyBoard).filter(CompanyBoard.slug == "sniffme").one()
        assert board.ats == "greenhouse"
        assert board.origin == "sniffed"
        assert board.source_host == "careers.sniffme.com"

    def test_sniffs_career_sites_linked_directly_by_non_aggregator_sources(self, db):
        """Remotive/RemoteOK/HN link straight at the employer — sniff those too."""
        from app.models.company_board import CompanyBoard
        from app.services.job_fetcher import fetch_and_save_jobs
        _make_profile_with_targets(db)
        job = _std_job(source="remotive", url="https://careers.directco.com/jobs/1",
                       source_job_id="RM1")

        with patch("app.services.ats_sniffer.sniff_host",
                   return_value={"lever": ["directco"]}) as sniff:
            with _patch_adapters([job]):
                fetch_and_save_jobs(db)

        assert sniff.call_args[0][0] == "careers.directco.com"
        board = db.query(CompanyBoard).filter(CompanyBoard.slug == "directco").one()
        assert board.origin == "sniffed"

    def test_aggregator_and_ats_urls_are_not_sniffed(self, db):
        from app.services.job_fetcher import fetch_and_save_jobs
        _make_profile_with_targets(db)
        jobs = [
            _std_job(source="indeed", url="https://www.indeed.com/viewjob?jk=1",
                     source_job_id="IN1"),
            _std_job(source="greenhouse", url="https://boards.greenhouse.io/x/jobs/1",
                     source_job_id="GH1", company="X"),
        ]
        with patch("app.services.ats_sniffer.sniff_host") as sniff:
            with _patch_adapters(jobs):
                fetch_and_save_jobs(db)
        sniff.assert_not_called()

    def test_registry_failure_does_not_block_the_fetch(self, db):
        from app.services.job_fetcher import fetch_and_save_jobs
        _make_profile_with_targets(db)
        with patch("app.services.company_boards.record_boards",
                   side_effect=RuntimeError("registry down")):
            with _patch_adapters([_std_job()]):
                result = fetch_and_save_jobs(db)
        assert result["inserted"] == 1


class TestOneTimeBackfillWiring:
    def test_runs_on_the_first_cycle_and_records_it(self, db):
        from app.services.job_fetcher import fetch_and_save_jobs
        profile = _make_profile_with_targets(db)
        with patch("app.services.board_backfill.backfill_boards") as backfill:
            from app.services.board_backfill import BackfillReport
            backfill.return_value = BackfillReport(jobs_scanned=12, boards_found=3)
            with _patch_adapters([]):
                fetch_and_save_jobs(db)

        assert backfill.call_count == 1
        state = profile.data["board_backfill"]
        assert state["done"] is True
        assert state["boards_found"] == 3
        assert profile.data["last_fetch"]["backfill"]["jobs_scanned"] == 12

    def test_never_runs_a_second_time(self, db):
        from app.services.job_fetcher import fetch_and_save_jobs
        _make_profile_with_targets(db)
        with patch("app.services.board_backfill.backfill_boards") as backfill:
            from app.services.board_backfill import BackfillReport
            backfill.return_value = BackfillReport()
            with _patch_adapters([]):
                fetch_and_save_jobs(db)
            with _patch_adapters([]):
                fetch_and_save_jobs(db)
        assert backfill.call_count == 1

    def test_recovered_boards_are_used_by_the_same_cycle(self, db):
        """The point of running it here rather than as a manual step."""
        from app.services.company_boards import record_boards
        from app.services.job_fetcher import fetch_and_save_jobs
        _make_profile_with_targets(db)

        def _backfill(session, **kwargs):
            from app.services.board_backfill import BackfillReport
            record_boards(session, {"lever": ["recovered"]}, origin="backfill")
            return BackfillReport(boards_found=1)

        with patch("app.services.board_backfill.backfill_boards", side_effect=_backfill):
            with patch("app.services.query_expansion.expand_search_queries",
                       return_value=(["Software Engineer"], None)):
                with patch("app.services.job_fetcher._run_all_adapters",
                           return_value=([], {})) as mock_run:
                    fetch_and_save_jobs(db)

        assert "recovered" in mock_run.call_args[0][3]["lever"]

    def test_a_failure_is_retried_then_given_up_on(self, db):
        from app.services.job_fetcher import _MAX_BACKFILL_ATTEMPTS, fetch_and_save_jobs
        profile = _make_profile_with_targets(db)
        with patch("app.services.board_backfill.backfill_boards",
                   side_effect=RuntimeError("network down")) as backfill:
            for _ in range(_MAX_BACKFILL_ATTEMPTS + 2):
                with _patch_adapters([]):
                    fetch_and_save_jobs(db)

        assert backfill.call_count == _MAX_BACKFILL_ATTEMPTS
        assert profile.data["board_backfill"]["done"] is False
        assert profile.data["board_backfill"]["attempts"] == _MAX_BACKFILL_ATTEMPTS

    def test_a_failure_does_not_block_the_fetch(self, db):
        from app.services.job_fetcher import fetch_and_save_jobs
        _make_profile_with_targets(db)
        with patch("app.services.board_backfill.backfill_boards",
                   side_effect=RuntimeError("network down")):
            with _patch_adapters([_std_job()]):
                result = fetch_and_save_jobs(db)
        assert result["inserted"] == 1

    def test_disabled_by_config(self, db):
        from app.config import settings
        from app.services.job_fetcher import fetch_and_save_jobs
        _make_profile_with_targets(db)
        with patch.object(settings, "BOARD_BACKFILL_ON_START", False):
            with patch("app.services.board_backfill.backfill_boards") as backfill:
                with _patch_adapters([]):
                    fetch_and_save_jobs(db)
        backfill.assert_not_called()


class TestSlugValidationWiring:
    def test_persists_slug_cache_and_report(self, db):
        from app.services.job_fetcher import fetch_and_save_jobs
        _make_profile_with_targets(db)
        fixed_report = {"greenhouse": {"fixed": {"Stripe Inc": "stripe"}, "invalid": ["badco"]}}
        with patch("app.services.ats_validation.validate_configured_slugs",
                   return_value=({"greenhouse": ["stripe"]},
                                 {"greenhouse": {"Stripe Inc": "stripe", "badco": None}},
                                 fixed_report)):
            with _patch_adapters([]):
                fetch_and_save_jobs(db)
        profile = db.query(Profile).first()
        assert profile.data["ats_slug_report"] == fixed_report
        assert profile.data["ats_slug_cache"]["greenhouse"]["badco"] is None

    def test_validation_failure_does_not_block_fetch(self, db):
        from app.services.job_fetcher import fetch_and_save_jobs
        _make_profile_with_targets(db)
        with patch("app.services.ats_validation.validate_configured_slugs",
                   side_effect=RuntimeError("network down")):
            with _patch_adapters([_std_job()]):
                result = fetch_and_save_jobs(db)
        assert result["inserted"] == 1


class TestSlugHarvestWiring:
    def test_harvested_slugs_feed_the_adapters(self, db):
        from app.services.job_fetcher import fetch_and_save_jobs
        _make_profile_with_targets(db)
        with patch("app.services.ats_discovery.harvest_slugs_from_lists",
                   return_value={"greenhouse": ["harvestedco"]}) as mock_harvest:
            with patch("app.services.query_expansion.expand_search_queries",
                       return_value=(["Software Engineer"], None)):
                with patch("app.services.job_fetcher._run_all_adapters",
                           return_value=([], {})) as mock_run:
                    fetch_and_save_jobs(db)
        assert mock_harvest.call_count == 1
        slug_map = mock_run.call_args[0][3]
        assert "harvestedco" in slug_map["greenhouse"]
        # harvested slugs also get persisted with the discovered set
        profile = db.query(Profile).first()
        assert "harvestedco" in profile.data["discovered_ats"]["greenhouse"]

    def test_harvest_failure_does_not_block_fetch(self, db):
        from app.services.job_fetcher import fetch_and_save_jobs
        _make_profile_with_targets(db)
        with patch("app.services.ats_discovery.harvest_slugs_from_lists",
                   side_effect=RuntimeError("github down")):
            with _patch_adapters([_std_job()]):
                result = fetch_and_save_jobs(db)
        assert result["inserted"] == 1


class TestFetchGroups:
    """
    One 47-minute task fetched everything, so Adzuna — an API call that could
    refresh hourly — ran on the schedule of a Chromium launch, and every
    posting arrived hours later than it could have.
    """

    def test_every_source_belongs_to_exactly_one_group(self):
        from app.routers.runs import TRIGGERABLE_SOURCES
        from app.services.job_fetcher import SOURCE_GROUPS

        seen = [s for group in SOURCE_GROUPS.values() for s in group]
        assert len(seen) == len(set(seen)), "a source is in two groups"
        # Every source the UI can trigger has a home, or the scheduled groups
        # would silently stop fetching it.
        assert set(TRIGGERABLE_SOURCES) == set(seen)

    def test_a_group_resolves_to_its_sources(self):
        from app.services.job_fetcher import SOURCE_GROUPS, group_sources

        assert group_sources("api") == set(SOURCE_GROUPS["api"])
        assert group_sources(None) is None
        assert group_sources("all") is None

    def test_an_unknown_group_is_refused_rather_than_silently_fetching_nothing(self):
        import pytest
        from app.services.job_fetcher import group_sources

        with pytest.raises(ValueError, match="Unknown fetch group"):
            group_sources("browsers")

    def test_a_group_run_only_calls_its_own_sources(self, db):
        from unittest.mock import patch
        from app.config import settings
        from app.services.job_fetcher import _run_all_adapters, group_sources

        _, stats = _run_all_adapters(
            ["SWE"], ["NYC"], settings, ats_slugs={},
            only=group_sources("browser"),
        )
        assert stats["adzuna"]["enabled"] is False
        assert stats["greenhouse"]["enabled"] is False

    def test_the_run_records_which_group_it_was(self, db):
        from unittest.mock import patch
        from app.models.fetch_run import FetchRun
        from app.models.profile import Profile
        from app.services.job_fetcher import fetch_and_save_jobs

        db.add(Profile(data={"target_roles": ["Backend Engineer"], "skills": {}}))
        db.commit()

        with patch("app.services.job_fetcher._run_all_adapters",
                   return_value=([], {})):
            result = fetch_and_save_jobs(db, group="api")

        assert result["group"] == "api"
        run = db.query(FetchRun).order_by(FetchRun.started_at.desc()).first()
        assert run.group == "api"

    def test_a_run_that_skipped_an_ats_does_not_count_it_as_empty(self, db):
        """
        The bug the split would have made routine: a run that never touched
        Greenhouse has no evidence about any Greenhouse board, and counting its
        silence would tick every one of them toward retirement — eight
        API-only cycles would have retired the whole registry.
        """
        from app.services.company_boards import record_boards
        from app.models.company_board import CompanyBoard
        from app.services.job_fetcher import _update_board_registry

        record_boards(db, {"greenhouse": ["acme"]}, origin="configured")
        db.flush()
        board = db.query(CompanyBoard).filter(CompanyBoard.slug == "acme").one()

        _update_board_registry(
            db, raw_jobs=[], ats_slugs={"greenhouse": ["acme"]},
            # An API-group run: greenhouse was not polled.
            source_stats={"greenhouse": {"count": 0, "errors": [], "enabled": False}},
            resolve_stats=None, updated_data={},
        )
        db.flush()
        db.refresh(board)
        assert board.consecutive_empty == 0

    def test_a_run_that_did_poll_an_ats_still_counts_its_silence(self, db):
        from app.services.company_boards import record_boards
        from app.models.company_board import CompanyBoard
        from app.services.job_fetcher import _update_board_registry

        record_boards(db, {"greenhouse": ["quiet"]}, origin="configured")
        db.flush()
        board = db.query(CompanyBoard).filter(CompanyBoard.slug == "quiet").one()

        _update_board_registry(
            db, raw_jobs=[], ats_slugs={"greenhouse": ["quiet"]},
            source_stats={"greenhouse": {"count": 0, "errors": [], "enabled": True}},
            resolve_stats=None, updated_data={},
        )
        db.flush()
        db.refresh(board)
        assert board.consecutive_empty == 1

    def test_each_group_has_its_own_lock(self):
        # A single key would have the hourly API run blocked by the
        # twice-daily browser tier, which is most of what the split was for.
        from app.tasks.fetch import GROUP_LOCK_KEYS

        assert len(set(GROUP_LOCK_KEYS.values())) == len(GROUP_LOCK_KEYS)

    def test_the_schedule_runs_the_groups_not_the_monolith(self):
        from app.celery_app import celery_app

        scheduled = {e["task"] for e in celery_app.conf.beat_schedule.values()}
        assert "app.tasks.fetch.fetch_api_sources" in scheduled
        assert "app.tasks.fetch.fetch_ats_boards" in scheduled
        assert "app.tasks.fetch.fetch_browser_tier" in scheduled
        # The combined task stays for the manual trigger, but nothing schedules
        # it — that would run everything twice.
        assert "app.tasks.fetch.fetch_jobs" not in scheduled


class TestTheSecondSourceContributesWhatTheFirstMissed:
    """
    Adzuna knows the title and rarely the pay; the employer's own board knows
    the pay, the employment type and the day it went up. Which one the schedule
    reached first decides nothing — the row should end up with both halves.
    """

    def cross_post(self, **extra):
        return _std_job(url="https://indeed.com/x", source_job_id=None,
                        source="indeed", **extra)

    def first(self, db, **extra):
        from app.services.job_fetcher import fetch_and_save_jobs
        _make_profile_with_targets(db)
        with _patch_adapters([_std_job(url="https://adzuna.com/x", **extra)]):
            fetch_and_save_jobs(db)

    def then(self, db, job):
        from app.services.job_fetcher import fetch_and_save_jobs
        with _patch_adapters([job]):
            return fetch_and_save_jobs(db)

    def test_the_pay_the_first_source_did_not_state(self, db):
        self.first(db)
        result = self.then(db, self.cross_post(
            salary_min=140000, salary_max=180000, salary_currency="USD"))
        assert result["merged"] == 1
        job = db.query(Job).one()
        assert (job.salary_min, job.salary_max) == (140000, 180000)

    def test_the_employment_type_the_first_source_did_not_state(self, db):
        self.first(db)
        self.then(db, self.cross_post(employment_type="contract"))
        assert db.query(Job).one().employment_type == "contract"

    def test_a_hand_cleared_salary_is_not_refilled(self, db):
        # This was the one automatic writer in the codebase that did not
        # consult `manual_fields`: a user who deleted a wrong salary by hand
        # had it put back on the next cycle.
        self.first(db, salary_min=90000)
        job = db.query(Job).one()
        job.salary_min = None
        job.salary_max = None
        job.manual_fields = ["salary_min"]
        db.commit()
        self.then(db, self.cross_post(salary_min=140000, salary_max=180000))
        assert db.query(Job).one().salary_min is None

    def test_a_repeat_of_a_job_we_already_know_everything_about(self, db):
        # Same URL, same text, nothing stated that we did not already have:
        # a skip, and not counted towards the cycle's "merged" total.
        from app.services.job_fetcher import fetch_and_save_jobs
        _make_profile_with_targets(db)
        job = _std_job(url="https://ex.com/same", source_job_id="SAME")
        with _patch_adapters([job]):
            fetch_and_save_jobs(db)
        with _patch_adapters([job]):
            again = fetch_and_save_jobs(db)
        assert again["merged"] == 0
        assert again["skipped"] == 1

    def test_a_refetch_carrying_new_pay_is_an_enrichment_not_a_skip(self, db):
        # The same posting again is not automatically nothing: a board that
        # added a salary band since last week is news, and reporting it as a
        # skip hid every late-arriving field the pipeline ever gained.
        from app.services.job_fetcher import fetch_and_save_jobs
        _make_profile_with_targets(db)
        with _patch_adapters([_std_job(url="https://ex.com/pay",
                                       source_job_id="PAY")]):
            fetch_and_save_jobs(db)
        with _patch_adapters([_std_job(url="https://ex.com/pay",
                                       source_job_id="PAY",
                                       salary_min=100000)]):
            again = fetch_and_save_jobs(db)
        assert again["merged"] == 1
        assert db.query(Job).one().salary_min == 100000


class TestAJobWeCouldNotStoreIsCountedAndNamed:
    """
    Every fetched posting used to end up in exactly one of four buckets —
    inserted, merged, skipped, stale — except the ones that raised on the way
    in. Those were logged as "error processing job" with no URL, no source and
    no title, and counted nowhere, so the cycle reported a fetched total that
    its own outcomes did not add up to and there was no way to find out which
    postings were missing.
    """

    def test_a_row_that_cannot_be_stored_is_counted_as_dropped(self, db):
        from app.services.job_fetcher import fetch_and_save_jobs
        _make_profile_with_targets(db)

        good = _std_job(url="https://ex.com/good", source_job_id="G1")
        # A board that ships its location as a nested object and an adapter
        # that passes it straight through: hashing it raises, the savepoint
        # rolls back that one row, and the rest of the batch still lands.
        bad = _std_job(url="https://ex.com/bad", source_job_id="B1",
                       location={"name": "NYC"}, source="jsearch")

        with _patch_adapters([good, bad]):
            result = fetch_and_save_jobs(db)

        assert result["inserted"] == 1
        assert result["dropped"] == 1
        assert result["per_source"]["jsearch"]["dropped"] == 1

    def test_the_log_line_names_the_posting_that_was_lost(self, db, caplog):
        import logging

        from app.services.job_fetcher import fetch_and_save_jobs
        _make_profile_with_targets(db)

        bad = _std_job(url="https://ex.com/bad", source_job_id="B1",
                       company="Vanishing Corp", location={"name": "NYC"})

        with caplog.at_level(logging.ERROR, logger="app.services.job_fetcher"):
            with _patch_adapters([bad]):
                fetch_and_save_jobs(db)

        dropped = "\n".join(r.getMessage() for r in caplog.records)
        assert "https://ex.com/bad" in dropped
        assert "Vanishing Corp" in dropped

    def test_the_outcomes_still_add_up_when_nothing_goes_wrong(self, db):
        from app.services.job_fetcher import fetch_and_save_jobs
        _make_profile_with_targets(db)

        with _patch_adapters([_std_job(url=f"https://ex.com/{n}", source_job_id=str(n))
                              for n in range(3)]):
            result = fetch_and_save_jobs(db)

        accounted = sum(result[key] for key in
                        ("inserted", "merged", "skipped", "stale", "dropped"))
        assert accounted == result["fetched"] == 3
