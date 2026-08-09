import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.models.application import Application
from app.models.job import Job, JobStatus
from app.models.outreach import Contact, OutreachMessage
from app.models.profile import Profile

PROFILE = {
    "personal": {"name": "Jane Doe", "email": "jane@example.com"},
    "narrative": {"summary": "Backend engineer."},
    "skills": {"languages": ["Python"]},
}


@pytest.fixture
def app_record(db):
    job = Job(source="greenhouse", title="Backend Engineer", company="Acme Corp",
              url="https://acme.com/jobs/1", description="Python and AWS.",
              fetched_at=datetime.now(timezone.utc), dedupe_hash=uuid.uuid4().hex,
              status=JobStatus.matched)
    db.add(job)
    db.flush()
    application = Application(job_id=job.id)
    db.add(application)
    db.add(Profile(data=PROFILE))
    db.flush()
    return application


@pytest.fixture
def contact(db, app_record):
    record = Contact(application_id=app_record.id, company="Acme Corp", company_key="acme",
                     name="Sam Recruiter", title="Technical Recruiter", email="sam@acme.com",
                     email_status="verified", role="recruiter", source="hunter")
    db.add(record)
    db.flush()
    return record


@pytest.fixture
def message(db, app_record, contact):
    record = OutreachMessage(contact_id=contact.id, application_id=app_record.id,
                             channel="email", subject="Backend Engineer",
                             body="Hi Sam, here is a draft.", status="draft")
    db.add(record)
    db.flush()
    return record


@pytest.fixture
def no_llm():
    """Every draft in these tests comes from the template path, not a provider."""
    with patch("app.services.outreach.generation_chat", return_value="Hi Sam, " + "word " * 30):
        yield


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

class TestOutreachPage:
    def test_renders_when_empty(self, client):
        response = client.get("/outreach")
        assert response.status_code == 200
        assert "Outreach" in response.text

    def test_shows_a_draft(self, client, message):
        response = client.get("/outreach")
        assert "Hi Sam, here is a draft." in response.text
        assert "Sam Recruiter" in response.text

    def test_separates_sent_from_drafts(self, client, db, message):
        message.status = "sent"
        message.sent_at = datetime.now(timezone.utc)
        db.flush()
        response = client.get("/outreach")
        assert "Sent, no reply yet" in response.text

    def test_lists_a_contact_with_no_message(self, client, contact):
        response = client.get("/outreach")
        assert "Contacts with no message" in response.text

    def test_says_sending_is_off(self, client):
        assert "turned off" in client.get("/outreach").text


class TestApplicationPanel:
    def test_detail_page_embeds_the_panel(self, client, app_record):
        response = client.get(f"/apps/{app_record.id}")
        assert response.status_code == 200
        assert 'id="outreach-panel"' in response.text
        assert "Find contacts" in response.text

    def test_panel_fragment_renders_a_contact(self, client, contact, app_record):
        response = client.get(f"/outreach/apps/{app_record.id}/panel")
        assert response.status_code == 200
        assert "sam@acme.com" in response.text
        assert "Technical Recruiter" in response.text

    def test_panel_shows_a_draft(self, client, message, app_record):
        response = client.get(f"/outreach/apps/{app_record.id}/panel")
        assert "Hi Sam, here is a draft." in response.text

    def test_panel_warns_about_an_earlier_conversation(self, client, db, app_record, contact):
        other_job = Job(source="lever", title="Platform Engineer", company="Acme, Inc.",
                        url="https://acme.com/jobs/2", fetched_at=datetime.now(timezone.utc),
                        dedupe_hash=uuid.uuid4().hex, status=JobStatus.matched)
        db.add(other_job)
        db.flush()
        other_app = Application(job_id=other_job.id)
        db.add(other_app)
        db.flush()
        earlier = Contact(application_id=other_app.id, company="Acme, Inc.",
                          company_key="acme", email="sam@acme.com", email_status="verified")
        db.add(earlier)
        db.flush()
        db.add(OutreachMessage(contact_id=earlier.id, application_id=other_app.id,
                               body="Hello.", status="sent",
                               sent_at=datetime.now(timezone.utc)))
        db.flush()
        response = client.get(f"/outreach/apps/{app_record.id}/panel")
        assert "Already written to about Platform Engineer" in response.text

    def test_panel_offers_linkedin_searches(self, client, app_record):
        response = client.get(f"/outreach/apps/{app_record.id}/panel")
        assert "Find people on LinkedIn" in response.text
        assert "Recruiters at Acme Corp" in response.text
        assert "linkedin.com/company/acme/people/" in response.text

    def test_panel_leads_with_alumni_when_the_profile_has_a_school(self, client, db, app_record):
        db.query(Profile).delete()
        db.add(Profile(data={**PROFILE, "education": [{"school": "Northeastern University"}]}))
        db.flush()
        response = client.get(f"/outreach/apps/{app_record.id}/panel")
        assert "Northeastern University alumni at Acme Corp" in response.text

    def test_a_contact_without_a_profile_gets_a_search_link(self, client, db, app_record):
        db.add(Contact(application_id=app_record.id, company="Acme Corp", company_key="acme",
                       name="Dana Lead", email="dana@acme.com", email_status="verified"))
        db.flush()
        response = client.get(f"/outreach/apps/{app_record.id}/panel")
        assert "Find on LinkedIn" in response.text
        assert "Dana+Lead" in response.text

    def test_a_github_contact_shows_its_profile_and_handle(self, client, db, app_record):
        db.add(Contact(application_id=app_record.id, company="Acme Corp", company_key="acme",
                       name="Ada Engineer", source="github",
                       profile_url="https://github.com/ada", twitter="ada"))
        db.flush()
        response = client.get(f"/outreach/apps/{app_record.id}/panel")
        assert "https://github.com/ada" in response.text
        assert "https://x.com/ada" in response.text

    def test_panel_404s_for_an_unknown_application(self, client):
        assert client.get(f"/outreach/apps/{uuid.uuid4()}/panel").status_code == 404

    def test_legacy_json_contacts_still_render(self, client, db, app_record):
        app_record.outreach_contacts = [
            {"name": "Old Contact", "email": "old@acme.com", "message": "An older draft."}
        ]
        db.flush()
        response = client.get(f"/apps/{app_record.id}")
        assert "Earlier outreach" in response.text
        assert "An older draft." in response.text


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

class TestDiscoverRoute:
    def test_queues_the_task_and_reports_progress(self, client, db, app_record):
        with patch("app.tasks.outreach.discover_contacts_task.delay") as delay:
            response = client.post(f"/outreach/apps/{app_record.id}/discover")
        assert response.status_code == 200
        delay.assert_called_once_with(str(app_record.id))
        db.refresh(app_record)
        assert app_record.outreach_status == "discovering"
        assert "Searching for contacts" in response.text

    def test_a_broker_failure_is_reported_not_left_spinning(self, client, db, app_record):
        with patch("app.tasks.outreach.discover_contacts_task.delay",
                   side_effect=Exception("broker down")):
            response = client.post(f"/outreach/apps/{app_record.id}/discover")
        db.refresh(app_record)
        assert app_record.outreach_status == "failed"
        assert "Could not queue" in response.text

    def test_refuses_a_second_concurrent_search(self, client, db, app_record):
        app_record.outreach_status = "discovering"
        app_record.outreach_checked_at = datetime.now(timezone.utc)
        db.flush()
        response = client.post(f"/outreach/apps/{app_record.id}/discover")
        assert "already running" in response.text

    def test_a_search_whose_worker_died_can_be_retried(self, client, db, app_record):
        app_record.outreach_status = "discovering"
        app_record.outreach_checked_at = datetime.now(timezone.utc) - timedelta(hours=2)
        db.flush()
        with patch("app.tasks.outreach.discover_contacts_task.delay") as delay:
            response = client.post(f"/outreach/apps/{app_record.id}/discover")
        delay.assert_called_once()
        assert "Searching for contacts" in response.text

    def test_the_panel_stops_polling_a_dead_search(self, client, db, app_record):
        app_record.outreach_status = "discovering"
        app_record.outreach_checked_at = datetime.now(timezone.utc) - timedelta(hours=2)
        db.flush()
        response = client.get(f"/outreach/apps/{app_record.id}/panel")
        assert "hx-trigger" not in response.text
        assert "never finished" in response.text

    def test_the_panel_polls_a_live_search(self, client, db, app_record):
        app_record.outreach_status = "discovering"
        app_record.outreach_checked_at = datetime.now(timezone.utc)
        db.flush()
        response = client.get(f"/outreach/apps/{app_record.id}/panel")
        assert 'hx-trigger="load delay:3s"' in response.text

    def test_404_for_an_unknown_application(self, client):
        assert client.post(f"/outreach/apps/{uuid.uuid4()}/discover").status_code == 404


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------

class TestContactRoutes:
    def test_adds_a_contact_by_hand(self, client, db, app_record):
        response = client.post(
            f"/outreach/apps/{app_record.id}/contacts",
            data={"name": "Dana Lead", "title": "Engineering Manager",
                  "email": "dana@acme.com", "role": "hiring_manager"},
        )
        assert response.status_code == 200
        assert "dana@acme.com" in response.text
        stored = db.query(Contact).filter(Contact.email == "dana@acme.com").one()
        assert stored.source == "manual"
        assert stored.first_name == "Dana"

    def test_rejects_a_contact_with_no_way_to_reach_them(self, client, app_record):
        response = client.post(f"/outreach/apps/{app_record.id}/contacts",
                               data={"name": "Nobody"})
        assert "needs an email or a LinkedIn URL" in response.text

    def test_rejects_a_malformed_address(self, client, app_record):
        response = client.post(f"/outreach/apps/{app_record.id}/contacts",
                               data={"name": "Dana", "email": "not-an-address"})
        assert "is not an email address" in response.text

    def test_updates_a_contact(self, client, db, contact):
        response = client.post(
            f"/outreach/contacts/{contact.id}/update",
            data={"name": "Sam R", "title": "Lead Recruiter", "email": "sam.r@acme.com",
                  "role": "recruiter", "notes": "Met at a meetup"},
        )
        assert response.status_code == 200
        db.refresh(contact)
        assert contact.email == "sam.r@acme.com"
        assert contact.notes == "Met at a meetup"

    def test_a_corrected_address_is_trusted_again(self, client, db, contact):
        contact.email_status = "guessed"
        db.flush()
        client.post(f"/outreach/contacts/{contact.id}/update",
                    data={"name": "Sam", "email": "sam.correct@acme.com"})
        db.refresh(contact)
        assert contact.email_status == "unverified"

    def test_archiving_hides_the_contact_and_drops_its_drafts(self, client, db, contact, message):
        response = client.post(f"/outreach/contacts/{contact.id}/archive")
        assert response.status_code == 200
        db.refresh(contact)
        db.refresh(message)
        assert contact.archived is True
        assert message.status == "skipped"
        assert "sam@acme.com" not in response.text

    def test_verification_needs_a_hunter_key(self, client, contact):
        response = client.post(f"/outreach/contacts/{contact.id}/verify")
        assert "HUNTER_IO_API_KEY" in response.text

    def test_verification_stores_the_result(self, client, db, contact):
        from app.routers import outreach as outreach_router

        with patch.object(outreach_router.settings, "HUNTER_IO_API_KEY", "key"):
            with patch("app.services.contact_finder.verify_email",
                       return_value={"status": "invalid", "confidence": 0}):
                response = client.post(f"/outreach/contacts/{contact.id}/verify")
        db.refresh(contact)
        assert contact.email_status == "invalid"
        assert "invalid" in response.text

    def test_404_for_an_unknown_contact(self, client):
        assert client.post(f"/outreach/contacts/{uuid.uuid4()}/archive").status_code == 404


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

class TestMessageRoutes:
    def test_drafting_a_message(self, client, db, contact, no_llm):
        response = client.post(f"/outreach/contacts/{contact.id}/draft",
                               data={"channel": "email", "kind": "initial", "tone": "warm"})
        assert response.status_code == 200
        assert db.query(OutreachMessage).filter(OutreachMessage.contact_id == contact.id).count() == 1

    def test_drafting_reports_a_failure_rather_than_500ing(self, client, contact):
        with patch("app.routers.outreach.draft_message", side_effect=Exception("no model")):
            response = client.post(f"/outreach/contacts/{contact.id}/draft", data={})
        assert response.status_code == 200
        assert "Could not write that draft" in response.text

    def test_saving_an_edit(self, client, db, message):
        response = client.post(f"/outreach/messages/{message.id}/save",
                               data={"subject": "New subject", "body": "Edited body."})
        assert response.status_code == 200
        db.refresh(message)
        assert message.body == "Edited body."
        assert message.edited is True

    def test_a_sent_message_cannot_be_edited(self, client, db, message):
        message.status = "sent"
        db.flush()
        response = client.post(f"/outreach/messages/{message.id}/save", data={"body": "x"})
        assert response.status_code == 409

    def test_regenerating_replaces_the_body(self, client, db, message):
        with patch("app.services.outreach.generation_chat", return_value="Rewritten, " + "x " * 30):
            response = client.post(f"/outreach/messages/{message.id}/regenerate",
                                   data={"feedback": "shorter"})
        assert response.status_code == 200
        db.refresh(message)
        assert message.body.startswith("Rewritten")
        assert message.feedback == "shorter"

    def test_regenerating_a_sent_message_is_refused_politely(self, client, db, message):
        message.status = "sent"
        db.flush()
        response = client.post(f"/outreach/messages/{message.id}/regenerate", data={})
        assert response.status_code == 200
        assert "already been sent" in response.text

    def test_marking_sent_starts_the_follow_up_clock(self, client, db, message):
        response = client.post(f"/outreach/messages/{message.id}/status", data={"status": "sent"})
        assert response.status_code == 200
        db.refresh(message)
        assert message.status == "sent"
        assert message.follow_up_due_at is not None

    def test_marking_replied_cancels_the_sequence(self, client, db, message):
        client.post(f"/outreach/messages/{message.id}/status", data={"status": "sent"})
        response = client.post(f"/outreach/messages/{message.id}/status", data={"status": "replied"})
        assert response.status_code == 200
        db.refresh(message)
        assert message.status == "replied"
        assert message.follow_up_due_at is None

    def test_an_unknown_status_is_rejected(self, client, message):
        response = client.post(f"/outreach/messages/{message.id}/status",
                               data={"status": "carrier-pigeon"})
        assert response.status_code == 422

    def test_deleting_a_draft(self, client, db, message):
        response = client.post(f"/outreach/messages/{message.id}/delete")
        assert response.status_code == 200
        assert db.query(OutreachMessage).filter(OutreachMessage.id == message.id).first() is None

    def test_a_sent_message_cannot_be_deleted(self, client, db, message):
        message.status = "sent"
        db.flush()
        assert client.post(f"/outreach/messages/{message.id}/delete").status_code == 409

    def test_sending_is_refused_while_smtp_is_off(self, client, message):
        response = client.post(f"/outreach/messages/{message.id}/send")
        assert response.status_code == 200
        assert "turned off" in response.text

    def test_sending_reports_success(self, client, db, message):
        from app.services import outreach_sender

        with patch.object(outreach_sender.settings, "OUTREACH_SEND_ENABLED", True), \
             patch.object(outreach_sender.settings, "SMTP_HOST", "smtp.example.com"), \
             patch.object(outreach_sender.settings, "SMTP_FROM_EMAIL", "jane@example.com"), \
             patch("app.services.outreach_sender._deliver"):
            response = client.post(f"/outreach/messages/{message.id}/send")
        assert "Sent to sam@acme.com" in response.text
        db.refresh(message)
        assert message.status == "sent"

    def test_404_for_an_unknown_message(self, client):
        assert client.post(f"/outreach/messages/{uuid.uuid4()}/delete").status_code == 404
