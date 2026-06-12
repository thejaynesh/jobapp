import uuid
import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
from fastapi import FastAPI

from app.models.job import JobStatus


def _make_job(status=JobStatus.matched, title="Backend Engineer", company="Acme"):
    job = MagicMock()
    job.id = uuid.uuid4()
    job.title = title
    job.company = company
    job.location = "Remote"
    job.is_remote = True
    job.url = "https://example.com/job"
    job.status = status
    job.llm_score = 85
    job.keyword_score = 0.8
    job.llm_reasoning = "Strong fit."
    job.matched_skills = ["Python", "FastAPI"]
    job.missing_skills = ["Rust"]
    job.source = "adzuna"
    job.fetched_at = None
    return job


# ---------------------------------------------------------------------------
# Jobs router tests
# ---------------------------------------------------------------------------

class TestJobsRouter:
    def _make_client(self, mock_db):
        from app.routers.jobs import router
        from app.database import get_db
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = lambda: mock_db
        return TestClient(app)

    def test_get_jobs_returns_200(self):
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [_make_job()]
        client = self._make_client(mock_db)
        response = client.get("/jobs")
        assert response.status_code == 200

    def test_get_jobs_html_contains_job_title(self):
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [_make_job()]
        client = self._make_client(mock_db)
        response = client.get("/jobs")
        assert "Backend Engineer" in response.text

    def test_get_jobs_filters_by_status(self):
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.order_by.return_value.all.return_value = []
        client = self._make_client(mock_db)
        response = client.get("/jobs?status=matched")
        assert response.status_code == 200

    def test_override_matched_to_filtered(self):
        job = _make_job(status=JobStatus.matched)
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = job
        client = self._make_client(mock_db)
        response = client.post(f"/jobs/{job.id}/override")
        assert response.status_code == 200
        assert job.status == JobStatus.filtered_out

    def test_override_filtered_to_matched(self):
        job = _make_job(status=JobStatus.filtered_out)
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = job
        client = self._make_client(mock_db)
        response = client.post(f"/jobs/{job.id}/override")
        assert response.status_code == 200
        assert job.status == JobStatus.matched

    def test_override_returns_404_for_missing_job(self):
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = None
        client = self._make_client(mock_db)
        response = client.post(f"/jobs/{uuid.uuid4()}/override")
        assert response.status_code == 404

    def test_get_jobs_no_jobs_shows_empty_state(self):
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.order_by.return_value.all.return_value = []
        client = self._make_client(mock_db)
        response = client.get("/jobs")
        assert response.status_code == 200

    def test_get_jobs_shows_company_name(self):
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [
            _make_job(company="GoodCorp")
        ]
        client = self._make_client(mock_db)
        response = client.get("/jobs")
        assert "GoodCorp" in response.text


# ---------------------------------------------------------------------------
# Apps router tests
# ---------------------------------------------------------------------------

class TestAppsRouter:
    def _make_app_obj(self, status="not_applied"):
        from app.models.application import ApplicationStatus
        app_obj = MagicMock()
        app_obj.id = uuid.uuid4()
        app_obj.status = ApplicationStatus(status)
        app_obj.notes = ""
        app_obj.applied_at = None
        app_obj.created_at = None
        app_obj.job = MagicMock()
        app_obj.job.id = uuid.uuid4()
        app_obj.job.title = "Backend Engineer"
        app_obj.job.company = "Acme"
        app_obj.job.url = "https://example.com"
        app_obj.documents = []
        return app_obj

    def _make_client(self, mock_db):
        from app.routers.apps import router
        from app.database import get_db
        fastapp = FastAPI()
        fastapp.include_router(router)
        fastapp.dependency_overrides[get_db] = lambda: mock_db
        return TestClient(fastapp)

    def test_get_apps_returns_200(self):
        mock_db = MagicMock()
        mock_db.query.return_value.order_by.return_value.all.return_value = [self._make_app_obj()]
        client = self._make_client(mock_db)
        response = client.get("/apps")
        assert response.status_code == 200

    def test_get_apps_shows_job_title(self):
        mock_db = MagicMock()
        mock_db.query.return_value.order_by.return_value.all.return_value = [self._make_app_obj()]
        client = self._make_client(mock_db)
        response = client.get("/apps")
        assert "Backend Engineer" in response.text

    def test_update_status_returns_200(self):
        app_obj = self._make_app_obj()
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = app_obj
        client = self._make_client(mock_db)
        response = client.post(f"/apps/{app_obj.id}/status", data={"status": "applied"})
        assert response.status_code == 200

    def test_update_status_changes_status(self):
        from app.models.application import ApplicationStatus
        app_obj = self._make_app_obj(status="not_applied")
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = app_obj
        client = self._make_client(mock_db)
        client.post(f"/apps/{app_obj.id}/status", data={"status": "applied"})
        assert app_obj.status == ApplicationStatus.applied

    def test_update_status_returns_404_for_missing(self):
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = None
        client = self._make_client(mock_db)
        response = client.post(f"/apps/{uuid.uuid4()}/status", data={"status": "applied"})
        assert response.status_code == 404

    def test_empty_apps_shows_empty_state(self):
        mock_db = MagicMock()
        mock_db.query.return_value.order_by.return_value.all.return_value = []
        client = self._make_client(mock_db)
        response = client.get("/apps")
        assert response.status_code == 200

    def test_update_status_rejects_invalid_status(self):
        app_obj = self._make_app_obj()
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = app_obj
        client = self._make_client(mock_db)
        response = client.post(f"/apps/{app_obj.id}/status", data={"status": "not_a_real_status"})
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# Settings router tests
# ---------------------------------------------------------------------------

class TestSettingsRouter:
    def _make_client(self, mock_db):
        from app.routers.settings import router
        from app.database import get_db
        fastapp = FastAPI()
        fastapp.include_router(router)
        fastapp.dependency_overrides[get_db] = lambda: mock_db
        return TestClient(fastapp)

    def _mock_profile(self):
        profile = MagicMock()
        profile.data = {
            "settings": {
                "min_match_score": 70,
                "fetch_interval_hours": 5,
                "min_keyword_skills": 2,
            }
        }
        return profile

    def test_get_settings_returns_200(self):
        mock_db = MagicMock()
        profile = self._mock_profile()
        with patch("app.routers.settings.get_or_create_profile", return_value=profile):
            client = self._make_client(mock_db)
            response = client.get("/settings")
        assert response.status_code == 200

    def test_get_settings_shows_current_values(self):
        mock_db = MagicMock()
        profile = self._mock_profile()
        with patch("app.routers.settings.get_or_create_profile", return_value=profile):
            client = self._make_client(mock_db)
            response = client.get("/settings")
        assert "70" in response.text

    def test_post_settings_saves_values(self):
        mock_db = MagicMock()
        profile = self._mock_profile()
        with patch("app.routers.settings.get_or_create_profile", return_value=profile):
            client = self._make_client(mock_db)
            response = client.post("/settings", data={
                "min_match_score": "80",
                "fetch_interval_hours": "3",
                "min_keyword_skills": "3",
            })
        assert response.status_code == 200
        assert profile.data["settings"]["min_match_score"] == 80

    def test_post_settings_returns_200(self):
        mock_db = MagicMock()
        profile = self._mock_profile()
        with patch("app.routers.settings.get_or_create_profile", return_value=profile):
            client = self._make_client(mock_db)
            response = client.post("/settings", data={
                "min_match_score": "75",
                "fetch_interval_hours": "5",
                "min_keyword_skills": "2",
            })
        assert response.status_code == 200

    def test_get_settings_with_no_profile_settings(self):
        mock_db = MagicMock()
        profile = MagicMock()
        profile.data = {}
        with patch("app.routers.settings.get_or_create_profile", return_value=profile):
            client = self._make_client(mock_db)
            response = client.get("/settings")
        assert response.status_code == 200
        assert "70" in response.text  # default value shown
