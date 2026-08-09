import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.models.application import Application
from app.models.job import Job, JobStatus
from app.models.outreach import Contact
from app.tasks import outreach as outreach_tasks


@pytest.fixture
def application(db):
    job = Job(source="greenhouse", title="Backend Engineer", company="Acme Corp",
              url="https://acme.com/jobs/1", fetched_at=datetime.now(timezone.utc),
              dedupe_hash=uuid.uuid4().hex, status=JobStatus.matched)
    db.add(job)
    db.flush()
    record = Application(job_id=job.id)
    db.add(record)
    db.flush()
    return record


@pytest.fixture
def session(db):
    """Point the tasks' own session factory at the test transaction."""
    with patch("app.tasks.outreach.SessionLocal", return_value=db):
        with patch.object(db, "close"):
            yield db


class TestDiscoverContactsTask:
    def test_marks_done_and_returns_a_count(self, session, application):
        contacts = [MagicMock(spec=Contact)]
        with patch("app.services.outreach.run_outreach", return_value=contacts):
            result = outreach_tasks.discover_contacts_task(str(application.id))
        assert result == {"status": "ok", "application_id": str(application.id), "contacts": 1}
        assert application.outreach_status == "done"

    def test_records_the_failure_on_the_application(self, session, application):
        with patch("app.services.outreach.run_outreach", side_effect=Exception("hunter down")):
            result = outreach_tasks.discover_contacts_task(str(application.id))
        assert result["status"] == "error"
        assert application.outreach_status == "failed"
        assert "hunter down" in application.outreach_error

    def test_missing_application(self, session):
        assert outreach_tasks.discover_contacts_task(str(uuid.uuid4()))["status"] == "not_found"

    def test_a_timeout_is_recorded_not_swallowed(self, session, application):
        from celery.exceptions import SoftTimeLimitExceeded

        with patch("app.services.outreach.run_outreach", side_effect=SoftTimeLimitExceeded()):
            result = outreach_tasks.discover_contacts_task(str(application.id))
        assert result["status"] == "timeout"
        assert application.outreach_status == "failed"


class TestProcessFollowups:
    def test_drafts_what_is_due(self, session):
        with patch("app.services.outreach.draft_due_follow_ups", return_value=[1, 2]):
            assert outreach_tasks.process_followups() == {"status": "ok", "drafted": 2}

    def test_disabled_when_auto_drafting_is_off(self, session):
        from app.config import settings

        with patch.object(settings, "OUTREACH_AUTO_DRAFT_FOLLOWUPS", False):
            assert outreach_tasks.process_followups() == {"status": "disabled"}

    def test_disabled_when_outreach_is_off(self, session):
        from app.config import settings

        with patch.object(settings, "OUTREACH_ENABLED", False):
            assert outreach_tasks.process_followups() == {"status": "disabled"}

    def test_a_failure_is_reported_not_raised(self, session):
        with patch("app.services.outreach.draft_due_follow_ups", side_effect=Exception("boom")):
            assert outreach_tasks.process_followups()["status"] == "error"


class TestSendMessageTask:
    def _message(self, db, application):
        from app.models.outreach import OutreachMessage

        contact = Contact(application_id=application.id, company="Acme Corp",
                          company_key="acme", email="sam@acme.com", email_status="verified")
        db.add(contact)
        db.flush()
        message = OutreachMessage(contact_id=contact.id, application_id=application.id,
                                  channel="email", body="Hi Sam.", status="draft")
        db.add(message)
        db.flush()
        return message

    def test_delivers_and_reports_ok(self, session, application):
        message = self._message(session, application)
        with patch("app.services.outreach_sender.send_message") as send:
            result = outreach_tasks.send_message_task(str(message.id))
        send.assert_called_once()
        assert result == {"status": "ok", "message_id": str(message.id)}

    def test_a_send_error_comes_back_as_a_message_not_a_crash(self, session, application):
        from app.services.outreach_sender import SendError

        message = self._message(session, application)
        with patch("app.services.outreach_sender.send_message",
                   side_effect=SendError("The mail server refused the login")):
            result = outreach_tasks.send_message_task(str(message.id))
        assert result == {"status": "error", "error": "The mail server refused the login"}

    def test_missing_message(self, session):
        assert outreach_tasks.send_message_task(str(uuid.uuid4()))["status"] == "not_found"


class TestBeatSchedule:
    def test_follow_ups_are_scheduled(self):
        from app.celery_app import celery_app

        assert "draft-due-outreach-followups" in celery_app.conf.beat_schedule

    def test_the_outreach_module_is_registered(self):
        from app.celery_app import celery_app

        assert "app.tasks.outreach" in celery_app.conf.include
