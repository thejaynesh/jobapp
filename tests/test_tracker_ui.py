import uuid
from datetime import datetime, timezone

import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
from fastapi import FastAPI

from app.models.job import JobStatus


def _make_job_model(**overrides):
    """
    A real (unsaved) Job rather than a MagicMock, for templates that render one.

    The application detail page reads a dozen job fields and does real work with
    them — `llm_score >= 75`, `posted_at.strftime(...)`, `status.value`. On a
    MagicMock each of those is a Mock that raises or renders as garbage, and
    stubbing them one at a time only defers the breakage to the next field
    someone adds to the template.
    """
    from app.models.job import Job
    fields = {
        "id": uuid.uuid4(),
        "source": "linkedin",
        "source_job_id": "J1",
        "source_urls": ["https://example.com"],
        "title": "Backend Engineer",
        "company": "Acme",
        "location": "New York, NY",
        "is_remote": False,
        "url": "https://example.com",
        "apply_url": None,
        "description": "Build things.",
        "experience_level": "mid",
        "keyword_score": None,
        "llm_score": None,
        "llm_reasoning": None,
        "matched_by": None,
        "matched_skills": [],
        "missing_skills": [],
        "status": JobStatus.matched,
        "fetched_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
        "posted_at": None,
        "dedupe_hash": "h1",
    }
    fields.update(overrides)
    return Job(**fields)


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

    @staticmethod
    def _mock_jobs_query(mock_db, jobs):
        """Self-chaining query mock supporting filter/count/order_by/offset/limit."""
        query = MagicMock()
        query.filter.return_value = query
        query.count.return_value = len(jobs)
        query.order_by.return_value.offset.return_value.limit.return_value.all.return_value = jobs
        mock_db.query.return_value = query

    def test_get_jobs_returns_200(self):
        mock_db = MagicMock()
        self._mock_jobs_query(mock_db, [_make_job()])
        client = self._make_client(mock_db)
        response = client.get("/jobs")
        assert response.status_code == 200

    def test_get_jobs_html_contains_job_title(self):
        mock_db = MagicMock()
        self._mock_jobs_query(mock_db, [_make_job()])
        client = self._make_client(mock_db)
        response = client.get("/jobs")
        assert "Backend Engineer" in response.text

    def test_get_jobs_filters_by_status(self):
        mock_db = MagicMock()
        self._mock_jobs_query(mock_db, [])
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
        self._mock_jobs_query(mock_db, [])
        client = self._make_client(mock_db)
        response = client.get("/jobs")
        assert response.status_code == 200

    def test_get_jobs_shows_company_name(self):
        mock_db = MagicMock()
        self._mock_jobs_query(mock_db, [_make_job(company="GoodCorp")])
        client = self._make_client(mock_db)
        response = client.get("/jobs")
        assert "GoodCorp" in response.text


# ---------------------------------------------------------------------------
# Apps router tests
# ---------------------------------------------------------------------------

class TestRunsPage:
    def _record(self, db, **kwargs):
        from app.services.fetch_history import record_run
        counts = {"fetched": 12, "inserted": 4, "merged": 1, "skipped": 7, "stale": 0}
        sources = {
            "linkedin": {"count": 12, "errors": [], "enabled": True},
            "indeed": {"count": 0, "errors": ["Indeed RSS fetch error: 403"],
                       "enabled": True},
        }
        run = record_run(
            db, datetime(2026, 8, 3, 6, 0, tzinfo=timezone.utc), counts, sources,
            per_source_outcome={"linkedin": {"inserted": 4, "merged": 1,
                                             "skipped": 7, "stale": 0}},
            queries=["software engineer"], locations=["New York, NY"],
            **kwargs,
        )
        db.commit()
        return run

    def test_empty_history_explains_itself(self, db, client):
        response = client.get("/runs")
        assert response.status_code == 200
        assert "No runs recorded yet" in response.text

    def test_lists_a_recorded_run(self, db, client):
        self._record(db)
        response = client.get("/runs")
        assert response.status_code == 200
        assert "Aug 03 06:00" in response.text

    def test_shows_each_source_and_its_failure_reason(self, db, client):
        self._record(db)
        response = client.get("/runs")
        assert "linkedin" in response.text
        assert "indeed" in response.text
        assert "Indeed RSS fetch error: 403" in response.text

    def test_shows_the_source_contribution_rollup(self, db, client):
        self._record(db)
        response = client.get("/runs")
        assert "Source contribution" in response.text

    def test_shows_the_queries_the_run_actually_searched(self, db, client):
        self._record(db)
        response = client.get("/runs")
        assert "software engineer" in response.text

    def test_top_boards_leaderboard_lists_producing_boards(self, db, client):
        from app.models.company_board import CompanyBoard
        from app.services.company_boards import record_boards, record_fetch_results
        self._record(db)
        record_boards(db, {"greenhouse": ["topco"]}, origin="discovered")
        record_fetch_results(db, "greenhouse", ["topco"], {"topco": 9})
        db.commit()

        response = client.get("/runs")
        assert "Top company boards" in response.text
        assert "topco" in response.text

    def test_page_survives_missing_history(self, db, client):
        with patch("app.services.fetch_history.recent_runs",
                   side_effect=RuntimeError("no table")):
            response = client.get("/runs")
        assert response.status_code == 200

    def test_limit_is_clamped(self, db, client):
        self._record(db)
        assert client.get("/runs?limit=99999").status_code == 200
        assert client.get("/runs?limit=0").status_code == 200


class TestRetiredBoardVisibility:
    """A board we stopped polling has to be visible, not silently dropped."""

    def _retire(self, db, slug="deadco", ats="greenhouse"):
        from app.models.company_board import CompanyBoard
        from app.services.company_boards import record_boards
        record_boards(db, {ats: [slug]}, origin="discovered")
        board = (
            db.query(CompanyBoard)
            .filter(CompanyBoard.ats == ats, CompanyBoard.slug == slug)
            .one()
        )
        board.active = False
        board.consecutive_empty = 8
        db.commit()
        return board

    def test_retired_board_is_listed_as_not_working(self, db, client):
        self._retire(db)
        response = client.get("/settings")
        assert response.status_code == 200
        assert "Boards not working" in response.text
        assert "deadco" in response.text

    def test_active_boards_are_not_in_the_not_working_list(self, db, client):
        from app.services.company_boards import record_boards
        record_boards(db, {"lever": ["healthyco"]}, origin="discovered")
        db.commit()
        response = client.get("/settings")
        assert "Boards not working" not in response.text

    def test_summary_counts_the_retired_board(self, db, client):
        self._retire(db)
        response = client.get("/settings")
        assert "Not working" in response.text

    def test_retry_puts_the_board_back_into_rotation(self, db, client):
        board = self._retire(db)
        response = client.post(f"/settings/boards/{board.id}/reactivate")
        assert response.status_code == 200
        db.refresh(board)
        assert board.active is True
        assert board.consecutive_empty == 0

    def test_retry_on_an_unknown_board_is_a_404(self, db, client):
        response = client.post(f"/settings/boards/{uuid.uuid4()}/reactivate")
        assert response.status_code == 404

    def test_page_survives_a_registry_failure(self, db, client):
        with patch("app.services.company_boards.retired_boards",
                   side_effect=RuntimeError("db gone")):
            response = client.get("/settings")
        assert response.status_code == 200


class TestAppsRouter:
    def _make_app_obj(self, status="not_applied"):
        from app.models.application import ApplicationStatus
        app_obj = MagicMock()
        app_obj.id = uuid.uuid4()
        app_obj.status = ApplicationStatus(status)
        app_obj.notes = ""
        app_obj.applied_at = None
        app_obj.created_at = None
        app_obj.job = _make_job_model()
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


# ---------------------------------------------------------------------------
# App detail page tests
# ---------------------------------------------------------------------------

class TestAppDetailRouter:
    def _make_app_obj(self):
        from app.models.application import ApplicationStatus, DocType
        app_obj = MagicMock()
        app_obj.id = uuid.uuid4()
        app_obj.status = ApplicationStatus.not_applied
        app_obj.notes = "some notes"
        app_obj.applied_at = None
        app_obj.created_at = None
        app_obj.outreach_contacts = []
        app_obj.job = _make_job_model(
            location="Remote", is_remote=True,
            description="We need a backend engineer.",
        )
        app_obj.documents = []
        return app_obj

    def _make_client(self, mock_db):
        from app.routers.apps import router
        from app.database import get_db
        fastapp = FastAPI()
        fastapp.include_router(router)
        fastapp.dependency_overrides[get_db] = lambda: mock_db
        return TestClient(fastapp)

    def test_get_detail_returns_200(self):
        app_obj = self._make_app_obj()
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = app_obj
        client = self._make_client(mock_db)
        response = client.get(f"/apps/{app_obj.id}")
        assert response.status_code == 200

    def test_get_detail_shows_job_title(self):
        app_obj = self._make_app_obj()
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = app_obj
        client = self._make_client(mock_db)
        response = client.get(f"/apps/{app_obj.id}")
        assert "Backend Engineer" in response.text

    def test_get_detail_returns_404_for_missing(self):
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = None
        client = self._make_client(mock_db)
        response = client.get(f"/apps/{uuid.uuid4()}")
        assert response.status_code == 404

    def test_save_notes_returns_200(self):
        app_obj = self._make_app_obj()
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = app_obj
        client = self._make_client(mock_db)
        response = client.post(f"/apps/{app_obj.id}/notes", data={"notes": "Updated notes"})
        assert response.status_code == 200

    def test_save_notes_persists_value(self):
        app_obj = self._make_app_obj()
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = app_obj
        client = self._make_client(mock_db)
        client.post(f"/apps/{app_obj.id}/notes", data={"notes": "my note"})
        assert app_obj.notes == "my note"

    def test_save_notes_returns_404_for_missing(self):
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = None
        client = self._make_client(mock_db)
        response = client.post(f"/apps/{uuid.uuid4()}/notes", data={"notes": "x"})
        assert response.status_code == 404

    def test_regenerate_queues_task(self):
        app_obj = self._make_app_obj()
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = app_obj
        client = self._make_client(mock_db)
        with patch("app.routers.apps.generate_docs") as mock_task:
            mock_task.delay = MagicMock()
            response = client.post(f"/apps/{app_obj.id}/regenerate", data={"feedback": "be more concise"})
        assert response.status_code == 200
        mock_task.delay.assert_called_once_with(str(app_obj.id), feedback="be more concise")

    def test_regenerate_returns_404_for_missing(self):
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = None
        client = self._make_client(mock_db)
        with patch("app.routers.apps.generate_docs"):
            response = client.post(f"/apps/{uuid.uuid4()}/regenerate", data={"feedback": ""})
        assert response.status_code == 404

    def test_download_doc_returns_404_for_missing(self):
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = None
        client = self._make_client(mock_db)
        response = client.get(f"/apps/docs/{uuid.uuid4()}/download")
        assert response.status_code == 404

    def test_download_doc_returns_404_when_file_missing(self):
        from app.models.application import DocType
        doc = MagicMock()
        doc.path = "/storage/resumes/nonexistent.pdf"
        doc.doc_type = DocType.resume
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = doc
        client = self._make_client(mock_db)
        response = client.get(f"/apps/docs/{uuid.uuid4()}/download")
        assert response.status_code == 404
