import uuid
import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
from fastapi import FastAPI


# ---------------------------------------------------------------------------
# Task 1 — find_email
# ---------------------------------------------------------------------------

class TestFindEmail:
    def test_returns_email_on_success(self):
        from app.services.outreach import find_email
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data": {"email": "recruiter@acme.com", "score": 90}
        }
        with patch("app.services.outreach.httpx.get", return_value=mock_resp):
            email = find_email("Acme Corp", "acme.com", "fake-key")
        assert email == "recruiter@acme.com"

    def test_returns_none_when_no_email_found(self):
        from app.services.outreach import find_email
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": {}}
        with patch("app.services.outreach.httpx.get", return_value=mock_resp):
            email = find_email("Acme Corp", "acme.com", "fake-key")
        assert email is None

    def test_returns_none_on_api_error(self):
        from app.services.outreach import find_email
        with patch("app.services.outreach.httpx.get", side_effect=Exception("timeout")):
            email = find_email("Acme Corp", "acme.com", "fake-key")
        assert email is None

    def test_returns_none_when_api_key_empty(self):
        from app.services.outreach import find_email
        email = find_email("Acme Corp", "acme.com", "")
        assert email is None


# ---------------------------------------------------------------------------
# Task 2 — extract_domain + find_linkedin_contact
# ---------------------------------------------------------------------------

class TestExtractDomain:
    def test_extracts_from_url(self):
        from app.services.outreach import extract_domain
        assert extract_domain("https://www.acme.com/jobs/123") == "acme.com"

    def test_extracts_without_www(self):
        from app.services.outreach import extract_domain
        assert extract_domain("https://lever.co/acme") == "lever.co"

    def test_returns_empty_string_for_invalid(self):
        from app.services.outreach import extract_domain
        assert extract_domain("not-a-url") == ""

    def test_strips_www_prefix(self):
        from app.services.outreach import extract_domain
        assert extract_domain("https://www.example.com/page") == "example.com"


class TestFindLinkedinContact:
    def test_returns_empty_dict_without_cookie(self):
        from app.services.outreach import find_linkedin_contact
        result = find_linkedin_contact("Acme Corp", "Engineering", "")
        assert result == {}

    def test_returns_dict_type(self):
        from app.services.outreach import find_linkedin_contact
        result = find_linkedin_contact("Acme Corp", "Engineering", "")
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# Task 3 — draft_outreach_message + run_outreach
# ---------------------------------------------------------------------------

class TestDraftOutreachMessage:
    def _profile(self):
        return {
            "name": "Jane Doe",
            "narrative": {"summary": "Experienced backend engineer."},
            "skills": {"languages": ["Python", "Go"]},
        }

    def test_returns_string(self):
        from app.services.outreach import draft_outreach_message
        with patch("app.services.outreach.chat_completion", return_value="Hi John, I'd love to connect."):
            msg = draft_outreach_message(
                self._profile(), "John Smith", "Recruiter", "Backend Engineer", "Acme Corp",
                "fake-key", "http://fake", "fake-model",
            )
        assert isinstance(msg, str)
        assert len(msg) > 0

    def test_passes_correct_llm_args(self):
        from app.services.outreach import draft_outreach_message
        with patch("app.services.outreach.chat_completion", return_value="Hello.") as mock_cc:
            draft_outreach_message(
                self._profile(), "John", "Engineer", "SWE", "Acme",
                "my-key", "http://base", "my-model",
            )
        _, kwargs = mock_cc.call_args
        assert kwargs["api_key"] == "my-key"
        assert kwargs["model"] == "my-model"

    def test_returns_fallback_on_llm_error(self):
        from app.services.outreach import draft_outreach_message
        with patch("app.services.outreach.chat_completion", side_effect=Exception("fail")):
            msg = draft_outreach_message(
                self._profile(), "John", "Engineer", "SWE", "Acme",
                "key", "url", "model",
            )
        assert isinstance(msg, str)
        assert len(msg) > 0

    def test_fallback_includes_job_and_company(self):
        from app.services.outreach import draft_outreach_message
        with patch("app.services.outreach.chat_completion", side_effect=Exception("fail")):
            msg = draft_outreach_message(
                self._profile(), "John", "Recruiter", "Backend Engineer", "Acme Corp",
                "key", "url", "model",
            )
        assert "Acme Corp" in msg or "Backend Engineer" in msg


class TestRunOutreach:
    def _profile_data(self):
        return {
            "name": "Jane Doe",
            "narrative": {"summary": "Engineer."},
            "skills": {"languages": ["Python"]},
        }

    def _make_app(self):
        app = MagicMock()
        app.id = str(uuid.uuid4())
        app.outreach_contacts = []
        app.job = MagicMock()
        app.job.company = "Acme Corp"
        app.job.title = "Backend Engineer"
        app.job.url = "https://acme.com/jobs/123"
        return app

    def test_appends_contact_to_outreach_contacts(self):
        from app.services.outreach import run_outreach
        db = MagicMock()
        db.query.return_value.first.return_value = MagicMock(data=self._profile_data())
        app = self._make_app()
        with patch("app.services.outreach.find_email", return_value="hr@acme.com"):
            with patch("app.services.outreach.find_linkedin_contact", return_value={"name": "John", "title": "Recruiter"}):
                with patch("app.services.outreach.draft_outreach_message", return_value="Hi John."):
                    run_outreach(db, app)
        assert len(app.outreach_contacts) == 1
        assert app.outreach_contacts[0]["email"] == "hr@acme.com"

    def test_commits_after_update(self):
        from app.services.outreach import run_outreach
        db = MagicMock()
        db.query.return_value.first.return_value = MagicMock(data=self._profile_data())
        app = self._make_app()
        with patch("app.services.outreach.find_email", return_value=None):
            with patch("app.services.outreach.find_linkedin_contact", return_value={}):
                with patch("app.services.outreach.draft_outreach_message", return_value="Hi."):
                    run_outreach(db, app)
        db.commit.assert_called_once()

    def test_stores_message_in_contact(self):
        from app.services.outreach import run_outreach
        db = MagicMock()
        db.query.return_value.first.return_value = MagicMock(data=self._profile_data())
        app = self._make_app()
        with patch("app.services.outreach.find_email", return_value="hr@acme.com"):
            with patch("app.services.outreach.find_linkedin_contact", return_value={}):
                with patch("app.services.outreach.draft_outreach_message", return_value="Hi there."):
                    run_outreach(db, app)
        assert app.outreach_contacts[0]["message"] == "Hi there."

    def test_handles_null_email_gracefully(self):
        from app.services.outreach import run_outreach
        db = MagicMock()
        db.query.return_value.first.return_value = MagicMock(data=self._profile_data())
        app = self._make_app()
        with patch("app.services.outreach.find_email", return_value=None):
            with patch("app.services.outreach.find_linkedin_contact", return_value={}):
                with patch("app.services.outreach.draft_outreach_message", return_value="Hi."):
                    run_outreach(db, app)
        assert app.outreach_contacts[0]["email"] is None


# ---------------------------------------------------------------------------
# Task 4 — outreach endpoint
# ---------------------------------------------------------------------------

class TestOutreachEndpoint:
    def _make_client(self, mock_db):
        from app.routers.outreach import router
        from app.database import get_db
        fastapp = FastAPI()
        fastapp.include_router(router)
        fastapp.dependency_overrides[get_db] = lambda: mock_db
        return TestClient(fastapp)

    def test_returns_202_for_valid_app(self):
        app_id = uuid.uuid4()
        mock_db = MagicMock()
        mock_app = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = mock_app
        client = self._make_client(mock_db)
        with patch("app.routers.outreach.run_outreach"):
            response = client.post(f"/api/apps/{app_id}/outreach")
        assert response.status_code == 202

    def test_returns_404_for_missing_app(self):
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = None
        client = self._make_client(mock_db)
        response = client.post(f"/api/apps/{uuid.uuid4()}/outreach")
        assert response.status_code == 404

    def test_calls_run_outreach(self):
        mock_db = MagicMock()
        mock_app = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = mock_app
        client = self._make_client(mock_db)
        with patch("app.routers.outreach.run_outreach") as mock_ro:
            client.post(f"/api/apps/{uuid.uuid4()}/outreach")
        mock_ro.assert_called_once_with(mock_db, mock_app)

    def test_returns_ok_status_in_body(self):
        mock_db = MagicMock()
        mock_app = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = mock_app
        client = self._make_client(mock_db)
        with patch("app.routers.outreach.run_outreach"):
            response = client.post(f"/api/apps/{uuid.uuid4()}/outreach")
        assert response.json()["status"] == "ok"
