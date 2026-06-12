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

class TestFetchAndSaveJobs:
    def test_inserts_new_job(self, db):
        from app.services.job_fetcher import fetch_and_save_jobs
        _make_profile_with_targets(db)
        jobs = [_std_job()]
        with patch("app.services.job_fetcher._run_all_adapters", return_value=jobs):
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
        with patch("app.services.job_fetcher._run_all_adapters", return_value=jobs):
            r1 = fetch_and_save_jobs(db)
        with patch("app.services.job_fetcher._run_all_adapters", return_value=jobs):
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
        with patch("app.services.job_fetcher._run_all_adapters", return_value=[j1]):
            fetch_and_save_jobs(db)
        with patch("app.services.job_fetcher._run_all_adapters", return_value=[j2]):
            r2 = fetch_and_save_jobs(db)
        assert r2["merged"] == 1
        job = db.query(Job).first()
        assert "https://indeed.com/1" in job.source_urls

    def test_no_profile_returns_zeros(self, db):
        from app.services.job_fetcher import fetch_and_save_jobs
        result = fetch_and_save_jobs(db)
        assert result == {"fetched": 0, "inserted": 0, "merged": 0, "skipped": 0}

    def test_empty_target_roles_returns_zeros(self, db):
        from app.services.job_fetcher import fetch_and_save_jobs
        get_or_create_profile(db)
        db.flush()
        result = fetch_and_save_jobs(db)
        assert result == {"fetched": 0, "inserted": 0, "merged": 0, "skipped": 0}

    def test_adapter_error_does_not_crash(self, db):
        from app.services.job_fetcher import fetch_and_save_jobs
        _make_profile_with_targets(db)
        with patch("app.services.job_fetcher._run_all_adapters",
                   side_effect=RuntimeError("adapter exploded")):
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
        with patch("app.services.job_fetcher._run_all_adapters", return_value=jobs):
            result = fetch_and_save_jobs(db)
        assert result["inserted"] == 3
        assert result["merged"] == 0
        assert result["skipped"] == 0


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
