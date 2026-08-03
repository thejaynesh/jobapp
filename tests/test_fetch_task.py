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
             description="Build things.", source="adzuna") -> dict:
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
    }


# ---------------------------------------------------------------------------
# Orchestrator tests
# ---------------------------------------------------------------------------

def _patch_adapters(jobs=None, side_effect=None):
    """Patch the adapter runner (returns (jobs, stats)) and skip LLM query expansion."""
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
        stack.enter_context(patch(
            "app.services.job_fetcher._run_all_adapters", return_value=(jobs or [], {})))
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
        from app.celery_app import celery_app
        schedule = celery_app.conf.beat_schedule
        assert "fetch-jobs-every-5-hours" in schedule
        entry = schedule["fetch-jobs-every-5-hours"]
        assert entry["task"] == "app.tasks.fetch.fetch_jobs"

    def test_beat_schedule_interval_matches_config(self):
        from app.celery_app import celery_app
        from app.config import settings
        schedule = celery_app.conf.beat_schedule
        entry = schedule["fetch-jobs-every-5-hours"]
        expected_seconds = settings.FETCH_INTERVAL_HOURS * 3600
        assert entry["schedule"].seconds == expected_seconds


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
