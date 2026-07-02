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
        assert mock_run.call_args[0][3] == {"lever": ["netflix"]}
