import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.models.application import Application, ApplicationDocument, DocType
from app.models.job import Job, JobStatus
from app.models.outreach import Contact, OutreachMessage
from app.models.profile import Profile
from app.services import outreach_sender
from app.services.outreach_sender import (
    SendError, build_email, send_message, sending_blocked_reason, sending_configured,
    sends_today,
)

PROFILE = {"personal": {"name": "Jane Doe", "email": "jane@example.com"}}


def _fixtures(db, email="sam@acme.com", email_status="verified"):
    job = Job(source="greenhouse", title="Backend Engineer", company="Acme Corp",
              url="https://acme.com/jobs/1", fetched_at=datetime.now(timezone.utc),
              dedupe_hash=uuid.uuid4().hex, status=JobStatus.matched)
    db.add(job)
    db.flush()
    app = Application(job_id=job.id)
    db.add(app)
    db.flush()
    contact = Contact(application_id=app.id, company="Acme Corp", company_key="acme",
                      name="Sam Recruiter", email=email, email_status=email_status,
                      role="recruiter", source="hunter")
    db.add(contact)
    db.flush()
    message = OutreachMessage(contact_id=contact.id, application_id=app.id, channel="email",
                              subject="Backend Engineer", body="Hi Sam, hello.", status="draft")
    db.add(message)
    db.add(Profile(data=PROFILE))
    db.flush()
    return app, contact, message


@pytest.fixture
def smtp_on():
    """A configured, enabled mail server — the only state in which sending runs."""
    with patch.object(outreach_sender.settings, "OUTREACH_SEND_ENABLED", True), \
         patch.object(outreach_sender.settings, "SMTP_HOST", "smtp.example.com"), \
         patch.object(outreach_sender.settings, "SMTP_USERNAME", ""), \
         patch.object(outreach_sender.settings, "SMTP_FROM_EMAIL", "jane@example.com"):
        yield


class TestSendingGuards:
    def test_disabled_by_default(self):
        assert sending_configured() is False
        assert "turned off" in sending_blocked_reason()

    def test_enabled_without_a_host_is_still_blocked(self):
        with patch.object(outreach_sender.settings, "OUTREACH_SEND_ENABLED", True):
            assert sending_configured() is False
            assert "SMTP" in sending_blocked_reason()

    def test_configured_and_enabled_is_allowed(self, smtp_on):
        assert sending_configured() is True
        assert sending_blocked_reason() == ""


class TestBuildEmail:
    def test_addresses_the_contact_by_name(self, db):
        _, _, message = _fixtures(db)
        mail = build_email(message, PROFILE)
        assert mail["To"] == "Sam Recruiter <sam@acme.com>"

    def test_uses_the_profile_as_the_sender(self, db):
        _, _, message = _fixtures(db)
        assert "jane@example.com" in build_email(message, PROFILE)["From"]

    def test_falls_back_to_a_subject_when_there_is_none(self, db):
        _, _, message = _fixtures(db)
        message.subject = None
        assert "Acme Corp" in build_email(message, PROFILE)["Subject"]

    def test_refuses_without_a_sender_address(self, db):
        _, _, message = _fixtures(db)
        with patch.object(outreach_sender.settings, "SMTP_FROM_EMAIL", ""), \
             patch.object(outreach_sender.settings, "SMTP_USERNAME", ""):
            with pytest.raises(SendError):
                build_email(message, {})

    def test_attaches_a_document(self, db, tmp_path):
        _, _, message = _fixtures(db)
        pdf = tmp_path / "resume.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        mail = build_email(message, PROFILE, [str(pdf)])
        assert [p.get_filename() for p in mail.iter_attachments()] == ["resume.pdf"]

    def test_a_missing_attachment_does_not_break_the_message(self, db):
        _, _, message = _fixtures(db)
        mail = build_email(message, PROFILE, ["/nowhere/resume.pdf"])
        assert list(mail.iter_attachments()) == []


class TestSendMessage:
    def test_refuses_when_sending_is_disabled(self, db):
        _, _, message = _fixtures(db)
        with pytest.raises(SendError, match="turned off"):
            send_message(db, message)

    def test_refuses_a_non_email_channel(self, db, smtp_on):
        _, _, message = _fixtures(db)
        message.channel = "linkedin_note"
        with pytest.raises(SendError, match="by hand"):
            send_message(db, message)

    def test_refuses_a_contact_with_no_address(self, db, smtp_on):
        _, contact, message = _fixtures(db, email=None, email_status="unknown")
        with pytest.raises(SendError, match="no email"):
            send_message(db, message)

    def test_refuses_a_guessed_address_by_default(self, db, smtp_on):
        _, _, message = _fixtures(db, email_status="guessed")
        with pytest.raises(SendError, match="pattern guess"):
            send_message(db, message)

    def test_sends_a_guessed_address_when_told_to(self, db, smtp_on):
        _, _, message = _fixtures(db, email_status="guessed")
        with patch("app.services.outreach_sender._deliver") as deliver:
            send_message(db, message, allow_guessed=True)
        deliver.assert_called_once()
        assert message.status == "sent"

    def test_refuses_an_invalid_address(self, db, smtp_on):
        _, _, message = _fixtures(db, email_status="invalid")
        with pytest.raises(SendError, match="verification"):
            send_message(db, message)

    def test_refuses_to_send_twice(self, db, smtp_on):
        _, _, message = _fixtures(db)
        message.status = "sent"
        with pytest.raises(SendError, match="already been sent"):
            send_message(db, message)

    def test_respects_the_daily_cap(self, db, smtp_on):
        _, contact, message = _fixtures(db)
        db.add(OutreachMessage(contact_id=contact.id, channel="email", body="x",
                               status="sent", sent_at=datetime.now(timezone.utc)))
        db.flush()
        with patch.object(outreach_sender.settings, "OUTREACH_MAX_SENDS_PER_DAY", 1):
            with pytest.raises(SendError, match="Daily send limit"):
                send_message(db, message)

    def test_a_successful_send_schedules_the_follow_up(self, db, smtp_on):
        _, _, message = _fixtures(db)
        with patch("app.services.outreach_sender._deliver"):
            send_message(db, message)
        assert message.sent_at is not None
        assert message.follow_up_due_at is not None

    def test_a_failure_leaves_it_a_draft_with_the_reason(self, db, smtp_on):
        _, _, message = _fixtures(db)
        with patch("app.services.outreach_sender._deliver",
                   side_effect=SendError("The mail server refused the login")):
            with pytest.raises(SendError):
                send_message(db, message)
        assert message.status == "draft"
        assert "refused the login" in message.send_error

    def test_attaches_the_current_documents(self, db, smtp_on, tmp_path):
        app, _, message = _fixtures(db)
        pdf = tmp_path / "resume.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        db.add(ApplicationDocument(application_id=app.id, doc_type=DocType.resume,
                                   version=1, path=str(pdf), is_current=True))
        db.flush()
        with patch("app.services.outreach_sender._deliver") as deliver:
            send_message(db, message)
        mail = deliver.call_args.args[0]
        assert [p.get_filename() for p in mail.iter_attachments()] == ["resume.pdf"]

    def test_attachments_can_be_turned_off(self, db, smtp_on, tmp_path):
        app, _, message = _fixtures(db)
        pdf = tmp_path / "resume.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        db.add(ApplicationDocument(application_id=app.id, doc_type=DocType.resume,
                                   version=1, path=str(pdf), is_current=True))
        db.flush()
        with patch.object(outreach_sender.settings, "OUTREACH_ATTACH_DOCUMENTS", False):
            with patch("app.services.outreach_sender._deliver") as deliver:
                send_message(db, message)
        assert list(deliver.call_args.args[0].iter_attachments()) == []


class TestSendsToday:
    def test_counts_only_the_last_day(self, db):
        _, contact, _ = _fixtures(db)
        db.add_all([
            OutreachMessage(contact_id=contact.id, channel="email", body="x", status="sent",
                            sent_at=datetime.now(timezone.utc)),
            OutreachMessage(contact_id=contact.id, channel="email", body="y", status="sent",
                            sent_at=datetime.now(timezone.utc) - timedelta(days=3)),
        ])
        db.flush()
        assert sends_today(db) == 1

    def test_ignores_other_channels(self, db):
        _, contact, _ = _fixtures(db)
        db.add(OutreachMessage(contact_id=contact.id, channel="linkedin", body="x",
                               status="sent", sent_at=datetime.now(timezone.utc)))
        db.flush()
        assert sends_today(db) == 0


class TestDeliver:
    def _mail(self):
        from email.message import EmailMessage

        mail = EmailMessage()
        mail["To"] = "sam@acme.com"
        mail.set_content("hi")
        return mail

    def test_starttls_path(self, smtp_on):
        with patch("smtplib.SMTP") as smtp:
            outreach_sender._deliver(self._mail())
        smtp.return_value.starttls.assert_called_once()
        smtp.return_value.send_message.assert_called_once()

    def test_starttls_is_skipped_for_implicit_tls(self, smtp_on):
        with patch.object(outreach_sender.settings, "SMTP_USE_SSL", True):
            with patch("smtplib.SMTP_SSL") as smtp_ssl:
                outreach_sender._deliver(self._mail())
        smtp_ssl.return_value.starttls.assert_not_called()

    def test_logs_in_when_a_username_is_configured(self, smtp_on):
        with patch.object(outreach_sender.settings, "SMTP_USERNAME", "jane"), \
             patch.object(outreach_sender.settings, "SMTP_PASSWORD", "pw"):
            with patch("smtplib.SMTP") as smtp:
                outreach_sender._deliver(self._mail())
        smtp.return_value.login.assert_called_once_with("jane", "pw")

    def test_implicit_tls_uses_smtp_ssl(self, smtp_on):
        with patch.object(outreach_sender.settings, "SMTP_USE_SSL", True):
            with patch("smtplib.SMTP_SSL") as smtp_ssl:
                outreach_sender._deliver(self._mail())
        smtp_ssl.assert_called_once()

    def test_an_unreachable_server_becomes_a_send_error(self, smtp_on):
        with patch("smtplib.SMTP", side_effect=OSError("connection refused")):
            with pytest.raises(SendError, match="Could not reach"):
                outreach_sender._deliver(self._mail())

    def test_a_refused_recipient_becomes_a_send_error(self, smtp_on):
        import smtplib

        with patch("smtplib.SMTP") as smtp:
            smtp.return_value.send_message.side_effect = smtplib.SMTPRecipientsRefused({})
            with pytest.raises(SendError, match="refused the recipient"):
                outreach_sender._deliver(self._mail())
