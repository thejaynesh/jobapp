"""
The three things the overlay gained: attach, answer, and mark applied.

All three exist because of the same gap. The tracker knows everything about a
posting right up to the moment the user is actually filling in the employer's
form — and at that moment they are on a different tab, retyping answers they
have typed a hundred times, uploading a PDF they have to go and find, and
afterwards forgetting to record that they applied at all.
"""

import base64
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.models.application import (
    Application, ApplicationDocument, ApplicationStatus, DocType,
)
from app.models.job import Job, JobStatus
from app.models.profile import Profile
from app.services import screening

TOKEN = "test-agent-token-value"
POSTING_URL = "https://boards.greenhouse.io/globex/jobs/4242"


@pytest.fixture
def agent(client, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_ENABLED", True)
    monkeypatch.setattr(settings, "AGENT_TOKEN", TOKEN)
    monkeypatch.setattr(settings, "APP_PASSWORD", "irrelevant-but-required")
    monkeypatch.setattr(settings, "SECRET_KEY", "not-the-placeholder-value")
    return client


def auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def _job(db, url=POSTING_URL, **kwargs) -> Job:
    job = Job(
        source="greenhouse", source_urls=[url], title="Backend Engineer",
        company="Globex Industries", url=url, description="A posting.",
        status=JobStatus.matched, fetched_at=datetime.now(timezone.utc),
        dedupe_hash=uuid.uuid4().hex, **kwargs,
    )
    db.add(job)
    db.commit()
    return job


def _application(db, job, **kwargs) -> Application:
    application = Application(job_id=job.id, **kwargs)
    db.add(application)
    db.commit()
    return application


class TestAttachingTheResume:
    def _resume(self, db, application, tmp_path, body=b"%PDF-1.4 fake"):
        path = tmp_path / "resume_v1.pdf"
        path.write_bytes(body)
        doc = ApplicationDocument(
            application_id=application.id, doc_type=DocType.resume,
            version=1, path=str(path), is_current=True,
        )
        db.add(doc)
        db.commit()
        return doc

    def test_the_bytes_come_back_base64(self, agent, db, tmp_path):
        # A content script can fill a file input but cannot get the file: the
        # PDF is behind the agent token, and base64 in JSON is what survives
        # chrome.runtime.sendMessage, which cannot carry a Blob.
        job = _job(db)
        application = _application(db, job)
        self._resume(db, application, tmp_path, b"%PDF-1.4 real bytes")

        body = agent.get(
            f"/api/agent/resume?url={POSTING_URL}", headers=auth()
        ).json()

        assert body["ok"] is True
        assert base64.b64decode(body["data"]) == b"%PDF-1.4 real bytes"
        assert body["content_type"] == "application/pdf"
        assert body["size"] == len(b"%PDF-1.4 real bytes")

    def test_the_filename_names_the_company(self, agent, db, tmp_path):
        # The stored name is a UUID and a version number; the employer sees it.
        job = _job(db)
        application = _application(db, job)
        self._resume(db, application, tmp_path)

        body = agent.get(
            f"/api/agent/resume?url={POSTING_URL}", headers=auth()
        ).json()

        assert body["filename"] == "resume_Globex_Industries.pdf"

    def test_an_unknown_posting_says_so(self, agent, db):
        body = agent.get(
            "/api/agent/resume?url=https://example.com/nope", headers=auth()
        ).json()
        assert body["ok"] is False
        assert "tracker" in body["detail"]

    def test_a_job_with_no_documents_says_so(self, agent, db):
        job = _job(db)
        _application(db, job)
        body = agent.get(
            f"/api/agent/resume?url={POSTING_URL}", headers=auth()
        ).json()
        assert body["ok"] is False
        assert "No resume" in body["detail"]

    def test_a_missing_file_is_named_as_a_missing_file(self, agent, db):
        # The row outlives the file when a volume is remounted. Saying which is
        # the difference between a fixable problem and a mysterious one.
        job = _job(db)
        application = _application(db, job)
        db.add(ApplicationDocument(
            application_id=application.id, doc_type=DocType.resume, version=1,
            path="/nonexistent/resume.pdf", is_current=True,
        ))
        db.commit()

        body = agent.get(
            f"/api/agent/resume?url={POSTING_URL}", headers=auth()
        ).json()

        assert body["ok"] is False
        assert "missing on the server" in body["detail"]

    def test_a_superseded_version_is_not_offered(self, agent, db, tmp_path):
        job = _job(db)
        application = _application(db, job)
        old = self._resume(db, application, tmp_path, b"old")
        old.is_current = False
        current = tmp_path / "resume_v2.pdf"
        current.write_bytes(b"new")
        db.add(ApplicationDocument(
            application_id=application.id, doc_type=DocType.resume, version=2,
            path=str(current), is_current=True,
        ))
        db.commit()

        body = agent.get(
            f"/api/agent/resume?url={POSTING_URL}", headers=auth()
        ).json()

        assert base64.b64decode(body["data"]) == b"new"

    def test_it_needs_the_token(self, agent, db):
        assert agent.get(f"/api/agent/resume?url={POSTING_URL}").status_code == 401


class TestMarkingApplied:
    def test_it_records_the_application(self, agent, db):
        job = _job(db)
        application = _application(db, job)

        body = agent.post(
            "/api/agent/mark-applied", json={"url": POSTING_URL}, headers=auth()
        ).json()

        db.refresh(application)
        assert body == {"ok": True, "status": "applied", "changed": True}
        assert application.status == ApplicationStatus.applied
        assert application.applied_at is not None

    def test_pressing_it_twice_changes_nothing(self, agent, db):
        # The obvious thing to do when unsure whether the first press worked.
        when = datetime.now(timezone.utc) - timedelta(days=2)
        job = _job(db)
        application = _application(
            db, job, status=ApplicationStatus.applied, applied_at=when,
        )

        body = agent.post(
            "/api/agent/mark-applied", json={"url": POSTING_URL}, headers=auth()
        ).json()

        db.refresh(application)
        assert body["ok"] is True
        assert body["changed"] is False
        assert application.applied_at == when

    def test_it_never_walks_an_interview_backwards(self, agent, db):
        job = _job(db)
        application = _application(db, job, status=ApplicationStatus.interviewing)

        body = agent.post(
            "/api/agent/mark-applied", json={"url": POSTING_URL}, headers=auth()
        ).json()

        db.refresh(application)
        assert application.status == ApplicationStatus.interviewing
        assert body["status"] == "interviewing"

    def test_an_unknown_posting_says_so(self, agent, db):
        body = agent.post(
            "/api/agent/mark-applied", json={"url": "https://example.com/nope"},
            headers=auth(),
        ).json()
        assert body["ok"] is False

    def test_a_missing_url_is_a_400(self, agent, db):
        assert agent.post(
            "/api/agent/mark-applied", json={}, headers=auth()
        ).status_code == 400

    def test_it_needs_the_token(self, agent, db):
        assert agent.post(
            "/api/agent/mark-applied", json={"url": POSTING_URL}
        ).status_code == 401


class TestTheAnswerBank:
    def test_unset_answers_are_blank_not_absent(self, db):
        # A caller must never have to tell "not set" from "not in the profile
        # yet"; both are the empty string, and the autofill skips those.
        answers = screening.answers({})
        assert set(answers) == set(screening.KEYS)
        assert all(value == "" for value in answers.values())

    def test_unknown_keys_are_dropped_on_save(self, db):
        cleaned = screening.clean({"start_date": "Immediately", "ssn": "123"})
        assert "ssn" not in cleaned
        assert cleaned["start_date"] == "Immediately"

    def test_whitespace_is_collapsed(self, db):
        assert screening.clean({"start_date": "  two   weeks\n"})["start_date"] == \
            "two weeks"

    def test_the_answers_reach_the_autofill(self, agent, db):
        db.add(Profile(data={
            "personal": {"name": "Jane Doe", "email": "j@example.com"},
            "screening_answers": {
                "work_authorization": "Yes",
                "sponsorship_required": "Yes — F-1 OPT, will need H-1B",
                "start_date": "Two weeks from an offer",
                "salary_expectation": "120000",
                "referral_source": "Company careers page",
            },
        }))
        db.commit()

        body = agent.get("/api/agent/autofill-fields", headers=auth()).json()

        assert body["work_authorization"] == "Yes"
        assert body["salary_expectation"] == "120000"
        assert body["first_name"] == "Jane"

    def test_a_profile_that_never_answered_sends_blanks(self, agent, db):
        db.add(Profile(data={"personal": {"name": "Jane Doe"}}))
        db.commit()

        body = agent.get("/api/agent/autofill-fields", headers=auth()).json()

        for key in screening.KEYS:
            assert body[key] == ""

    def test_the_profile_page_saves_them(self, client, db):
        db.add(Profile(data={"personal": {}}))
        db.commit()

        response = client.post("/profile/screening", data={
            "work_authorization": "Yes",
            "sponsorship_required": "Yes, I will require sponsorship",
            "start_date": "Immediately",
            "salary_expectation": "120000",
            "referral_source": "LinkedIn",
        })

        assert response.status_code == 200
        profile = db.query(Profile).first()
        db.refresh(profile)
        assert profile.data["screening_answers"]["referral_source"] == "LinkedIn"

    def test_the_tab_renders(self, client, db):
        db.add(Profile(data={"personal": {}}))
        db.commit()
        body = client.get("/profile?tab=screening").text
        assert "Will you need sponsorship?" in body
        assert "How did you hear about us?" in body
