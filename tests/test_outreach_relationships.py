"""Regression coverage for evidence, shared identities and actual next actions."""
import uuid
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from unittest.mock import MagicMock, patch

import pytest

from app.models.application import Application, ApplicationStatus
from app.models.job import Job, JobStatus
from app.models.outreach import Contact, NetworkPerson, OutreachConversation, OutreachInteraction, OutreachMessage
from app.models.profile import Profile
from app.services import contact_finder, mailbox, networking, outreach, outreach_sender


def contact(db, email="sam@acme.com", company="Acme", **kwargs):
    job = Job(source="manual", title="Platform Engineer", company=company, url="https://acme.com/jobs/1",
              description="Platform APIs", dedupe_hash=uuid.uuid4().hex, status=JobStatus.matched,
              fetched_at=datetime.now(timezone.utc))
    db.add(job)
    db.flush()
    app = Application(job_id=job.id)
    db.add(app)
    db.flush()
    person = Contact(application_id=app.id, company=company, company_key=company.lower(), email=email,
        name="Sam Recruiter", role="recruiter", email_status="verified" if email else "unknown", **kwargs)
    db.add(person)
    db.flush()
    return person


def sent(db, person, **kwargs):
    message = OutreachMessage(contact_id=person.id, application_id=person.application_id,
        status="draft", body="Earlier platform role message", subject="Platform role", kind="initial",
        sequence_step=1, message_id=kwargs.pop("message_id", f"<{uuid.uuid4()}@example.com>"), **kwargs)
    db.add(message)
    db.flush()
    outreach.mark_sent(db, message, when=networking.now() - timedelta(days=8))
    return message


@pytest.fixture
def smtp_ready():
    with patch.object(outreach_sender.settings, "OUTREACH_SEND_ENABLED", True), \
         patch.object(outreach_sender.settings, "SMTP_HOST", "smtp.example.com"), \
         patch.object(outreach_sender.settings, "SMTP_FROM_EMAIL", "me@example.com"):
        yield


def draft(db, person, kind="initial"):
    message = OutreachMessage(contact_id=person.id, application_id=person.application_id, kind=kind,
        status="draft", channel="email", subject="Platform role", body="My carefully reviewed message")
    db.add(message)
    db.flush()
    return message


@pytest.mark.parametrize("status,expected", [("valid", "verified"), ("invalid", "invalid"), ("accept_all", "accept_all"), ("unknown", "unverified")])
def test_hunter_current_status_is_authoritative(status, expected):
    response = MagicMock(status_code=200, content=b"{}")
    response.json.return_value = {"data": {"status": status, "result": "deliverable", "score": 80}}
    with patch("app.services.contact_finder.httpx.get", return_value=response):
        assert contact_finder.verify_email("sam@acme.com", "key")["status"] == expected


def test_personal_hunter_address_does_not_mean_verified():
    found = contact_finder.hunter_contacts("acme.com", "key", data={"emails": [{"value": "sam@acme.com", "type": "personal"}]})
    assert found[0]["email_status"] == "unverified"


def test_pending_verification_is_explicit():
    with patch("app.services.contact_finder.httpx.get", return_value=MagicMock(status_code=202)):
        assert contact_finder.verify_email("sam@acme.com", "key") == {"pending": True}


def test_merge_keeps_real_verification_and_both_sources():
    records = outreach._dedupe([
        {"name": "Sam Recruiter", "email": "sam@acme.com", "email_status": "unverified", "evidence": {"sources": [{"uri": "https://acme.com/jobs"}]}},
        {"name": "Sam Recruiter", "email": "sam@acme.com", "email_status": "verified", "email_confidence": 97,
         "evidence": {"verification": {"status": "valid", "date": "2026-10-01"}, "sources": [{"uri": "https://acme.com/team"}]}},
    ])
    assert len(records) == 1 and records[0]["email_status"] == "verified"
    assert len(records[0]["evidence"]["sources"]) == 2


def test_same_name_with_distinct_emails_does_not_merge():
    assert len(outreach._dedupe([{"name": "Sam Smith", "email": "a@acme.com"}, {"name": "Sam Smith", "email": "b@acme.com"}])) == 2


def test_discovery_upsert_keeps_same_name_different_email_separate(db):
    first = contact(db, "one@acme.com")
    second = outreach.upsert_contact(db, first.application,
        {"name": first.name, "email": "two@acme.com", "source": "hunter", "email_status": "unverified"})
    db.flush()
    assert second.id != first.id and second.person_id != first.person_id


def test_responding_guessed_domain_is_not_ownership_evidence():
    from app.services.company_domain import resolve_company_domain
    with patch("app.services.company_domain.domain_responds", return_value=True):
        assert resolve_company_domain("Acme", url="https://jobs.lever.co/acme") == ("", "")


def test_description_substring_does_not_verify_another_company_domain():
    from app.services.company_domain import resolve_company_domain
    assert resolve_company_domain("Acme", description="Our partner https://acmeconsulting.org") == ("", "")


def test_github_company_name_inside_another_host_does_not_match():
    from app.services.github_contacts import _org_matches_company
    assert not _org_matches_company({"login": "acme", "name": "Acme", "blog": "https://acme.com.unrelated.org"}, "Acme", "acme.com")
    assert _org_matches_company({"login": "acme", "blog": "https://engineering.acme.com"}, "Acme", "acme.com")


def test_same_person_and_company_share_conversation_across_roles(db):
    first, second = contact(db), contact(db)
    one, two = networking.conversation_for(db, first), networking.conversation_for(db, second)
    assert one.id == two.id and first.person_id == second.person_id
    assert db.query(NetworkPerson).count() == 1


def test_substantive_company_names_have_separate_conversations(client, db):
    labs = contact(db, company="Acme Labs")
    systems = contact(db, company="Acme Systems")
    labs.company_key = systems.company_key = "acme"  # legacy search/discovery grouping
    one, two = networking.conversation_for(db, labs), networking.conversation_for(db, systems)
    db.commit()
    assert labs.person_id == systems.person_id and one.id != two.id
    assert one.company_key == "acme labs" and two.company_key == "acme systems"
    opened = client.get(f"/outreach/conversations/{two.id}", follow_redirects=False)
    assert opened.headers["location"] == f"/outreach/contacts/{systems.id}"


def test_company_case_and_spacing_are_normalized_without_erasing_words(db):
    first = contact(db, company=" Acme   Labs ")
    second = contact(db, company="acme labs")
    assert networking.conversation_for(db, first).id == networking.conversation_for(db, second).id


def test_names_alone_never_merge_people(db):
    first, second = contact(db, "one@acme.com"), contact(db, "two@acme.com")
    assert networking.ensure_person(db, first).id != networking.ensure_person(db, second).id


def test_linkedin_query_and_slash_variants_share_identity(db):
    first = contact(db, None, linkedin_url="https://linkedin.com/in/sam/?trk=search")
    second = contact(db, None, linkedin_url="https://www.linkedin.com/in/sam")
    assert networking.ensure_person(db, first).id == networking.ensure_person(db, second).id


def test_mailbox_sender_match_searches_all_contacts(db):
    contact(db)  # first matching row has no sent mail
    second = contact(db)
    message = sent(db, second)
    assert mailbox._message_by_sender(db, "sam@acme.com", networking.now()).id == message.id


def test_reply_pauses_followups_for_all_applications_of_person(db):
    first, second = contact(db), contact(db)
    original, other = sent(db, first), sent(db, second)
    draft = OutreachMessage(contact_id=second.id, application_id=second.application_id, kind="follow_up", status="draft", body="Follow up")
    db.add(draft)
    db.commit()
    outreach.mark_replied(db, original)
    db.refresh(other)
    db.refresh(draft)
    assert other.follow_up_due_at is None and draft.status == "skipped"
    assert original.conversation.status == "reply_needed"


def test_inbound_message_is_retained_once_and_used_as_context(db):
    person = contact(db)
    message = sent(db, person)
    incoming = EmailMessage()
    incoming["From"], incoming["Message-ID"], incoming["In-Reply-To"] = "sam@acme.com", "<inbound-unique@acme.com>", message.message_id
    incoming.set_content("Please send your API project summary.")
    counts = dict(replies=0, bounces=0, skipped=0)
    with patch("app.services.application_mail.propose"):
        mailbox._process(db, incoming, counts)
        mailbox._process(db, incoming, counts)
    assert db.query(OutreachInteraction).filter_by(message_id="<inbound-unique@acme.com>").count() == 1
    person.notes = "Met during the alumni event"
    context = networking.relationship_context(db, person)
    assert "API project summary" in context and "alumni event" in context


def test_followup_headers_link_to_sent_message(db):
    person = contact(db)
    original = sent(db, person)
    followup = OutreachMessage(contact=person, application=person.application, conversation=original.conversation,
        kind="follow_up", status="draft", body="Any update?", subject="Re: Platform role")
    mail = outreach_sender.build_email(followup, {"personal": {"email": "me@example.com"}})
    assert mail["In-Reply-To"] == original.message_id
    assert original.message_id in mail["References"]


def test_rejected_application_does_not_get_automatic_chaser(db):
    person = contact(db)
    message = sent(db, person)
    person.application.status = ApplicationStatus.rejected
    db.commit()
    with patch("app.services.outreach.draft_message") as generate:
        assert outreach.draft_due_follow_ups(db) == []
    generate.assert_not_called()
    assert message.follow_up_due_at is None


def test_failed_followup_has_bounded_retry_and_visible_next_action(db):
    person = contact(db)
    message = sent(db, person)
    with patch("app.services.outreach.draft_message", side_effect=RuntimeError("provider down")):
        outreach.draft_due_follow_ups(db)
    assert message.followup_attempts == 1 and message.follow_up_due_at > networking.now()
    assert "provider down" in message.followup_error
    assert "failed" in message.conversation.next_action


def test_cache_distinguishes_failed_empty_and_reuses_company_result(db):
    fetch = MagicMock(return_value=[])
    _, first = networking.cached_discovery(db, "acme", "acme.com", "team_page", fetch)
    _, second = networking.cached_discovery(db, "acme", "acme.com", "team_page", fetch)
    assert first["status"] == "empty" and second["cached"] and fetch.call_count == 1
    broken = MagicMock(side_effect=RuntimeError("quota exceeded"))
    _, result = networking.cached_discovery(db, "acme", "acme.com", "hunter", broken)
    assert result["status"] == "failed" and "quota" in result["error"]
    networking.cached_discovery(db, "acme", "acme.com", "hunter", broken)
    assert broken.call_count == 2


def test_role_fit_can_rank_a_platform_manager_above_generic_manager():
    generic = {"name": "Jane", "title": "Engineering Manager", "role": "hiring_manager", "email": "jane@acme.com"}
    targeted = {**generic, "title": "Platform Engineering Manager"}
    assert contact_finder.contact_score(targeted, {"title": "Platform Engineer"}) > contact_finder.contact_score(generic, {"title": "Platform Engineer"})


def test_capture_creates_standalone_relationship(client, db):
    response = client.post("/api/outreach/contacts/capture", json={"company": "Acme", "name": "Sam Recruiter",
        "email": "sam@acme.com", "notes": "Introduced by an old colleague", "relationship_kind": "introduced"})
    assert response.status_code == 201
    person = db.get(Contact, uuid.UUID(response.json()["contact_id"]))
    assert person.application_id is None and person.person.relationship_kind == "introduced"
    page = client.get(response.json()["url"])
    assert page.status_code == 200 and "Relationship context" in page.text


def test_repeated_capture_preserves_saved_context_and_provenance(client, db):
    original = client.post("/api/outreach/contacts/capture", json={"company": "Acme", "name": "Sam Recruiter",
        "email": "sam@acme.com", "title": "Platform Recruiter", "notes": "Old colleague", "relationship_kind": "colleague",
        "source_url": "https://acme.com/team"})
    again = client.post("/api/outreach/contacts/capture", json={"company": "Acme", "email": "sam@acme.com",
        "source_url": "https://acme.com/jobs"})
    assert again.json()["contact_id"] == original.json()["contact_id"]
    person = db.get(Contact, uuid.UUID(again.json()["contact_id"]))
    assert person.name == "Sam Recruiter" and person.title == "Platform Recruiter"
    assert person.notes == "Old colleague" and person.person.relationship_kind == "colleague"
    assert len(person.evidence["sources"]) == 2


def test_profile_only_lead_has_research_action_instead_of_unusable_draft(db):
    person = contact(db, None, profile_url="https://github.com/sam")
    with patch("app.services.outreach.discover_contacts", return_value=[person]), patch(
            "app.services.outreach.live", return_value=MagicMock(OUTREACH_ENABLED=True)), patch(
            "app.services.outreach.draft_message") as draft:
        outreach.run_outreach(db, person.application)
    draft.assert_not_called()
    assert "find a contact channel" in networking.conversation_for(db, person).next_action


def test_relationship_milestone_is_recorded_explicitly(client, db):
    person = contact(db)
    response = client.post(f"/outreach/contacts/{person.id}/relationship", data={"status": "referral_submitted",
        "relationship_kind": "introduced", "interaction": "Sam confirmed the referral in the portal",
        "next_action": "Thank Sam", "due": "2026-12-01"})
    assert response.status_code == 200
    conversation = networking.conversation_for(db, person)
    assert conversation.status == "referral_submitted" and conversation.next_action == "Thank Sam"
    assert outreach.outreach_stats(db)["referrals_submitted"] == 1


def test_search_filters_before_pagination(client, db):
    person = contact(db)
    old = OutreachMessage(contact_id=person.id, body="uniquely-find-this", status="draft",
                          created_at=networking.now() - timedelta(days=365))
    db.add(old)
    db.add_all([OutreachMessage(contact_id=person.id, body=f"Recent message {i}", status="draft") for i in range(405)])
    db.commit()
    response = client.get("/outreach", params={"q": "uniquely-find-this"})
    assert response.status_code == 200 and "uniquely-find-this" in response.text


def test_application_delete_preserves_relationship_and_sent_history(db):
    person = contact(db)
    message = sent(db, person)
    db.delete(person.application)
    db.commit()
    db.refresh(person)
    db.refresh(message)
    assert person.application_id is None and message.application_id is None
    assert message.conversation.person_id == person.person_id


def test_tracker_closing_role_cancels_chasers_but_keeps_human_action(db):
    from app.services import tracker
    person = contact(db)
    message = sent(db, person)
    conversation = message.conversation
    conversation.next_action = "Ask Sam about the next quarter's team plans"
    conversation.next_action_due_at = networking.now() + timedelta(days=30)
    draft = OutreachMessage(contact_id=person.id, application_id=person.application_id, conversation=conversation,
        kind="follow_up", status="draft", body="Still interested in the closed role")
    db.add(draft)
    db.commit()
    tracker.set_status(db, person.application, ApplicationStatus.rejected)
    assert message.follow_up_due_at is None and draft.status == "skipped"
    assert conversation.next_action.startswith("Ask Sam") and conversation.next_action_due_at is not None


@pytest.mark.parametrize("state", ["sending", "uncertain", "idle"])
def test_another_application_cannot_send_to_a_person_with_unresolved_delivery(db, smtp_ready, state):
    first, second = contact(db), contact(db)
    pending, next_message = draft(db, first), draft(db, second)
    pending.send_state, pending.message_id = state, "<unresolved@example.com>"
    pending.send_started_at = networking.now()
    networking.conversation_for(db, first)
    db.commit()
    with patch("app.services.outreach_sender._deliver") as deliver:
        with pytest.raises(outreach_sender.SendError, match="Another message to this person"):
            outreach_sender.send_message(db, next_message)
    deliver.assert_not_called()


def test_weekly_company_cap_includes_reserved_people(db, smtp_ready):
    first, second = contact(db), contact(db, "other@acme.com")
    pending, next_message = draft(db, first), draft(db, second)
    networking.conversation_for(db, first)
    pending.send_state, pending.message_id = "uncertain", "<unresolved@example.com>"
    db.commit()
    with patch.object(outreach_sender.settings, "OUTREACH_COMPANY_CONTACTS_PER_WEEK", 1), \
         patch("app.services.outreach_sender._deliver") as deliver:
        with pytest.raises(outreach_sender.SendError, match="weekly contact limit"):
            outreach_sender.send_message(db, next_message)
    deliver.assert_not_called()


def test_reply_to_older_conversation_is_not_blocked_by_new_people_quota(db, smtp_ready):
    person, other = contact(db), contact(db, "other@acme.com")
    original = sent(db, person)
    outreach.mark_replied(db, original)
    outreach.mark_sent(db, draft(db, other))  # occupies this week's one-person quota
    response = draft(db, person, "reply")
    db.commit()
    with patch.object(outreach_sender.settings, "OUTREACH_COMPANY_CONTACTS_PER_WEEK", 1), \
         patch("app.services.outreach_sender._deliver") as deliver:
        outreach_sender.send_message(db, response)
    deliver.assert_called_once()
    assert response.status == "sent" and response.follow_up_due_at is None


def test_reply_threads_to_inbound_and_bypasses_cold_cooldown_on_closed_role(db, smtp_ready):
    person = contact(db)
    original = sent(db, person)
    outreach.mark_replied(db, original)
    networking.record_interaction(db, original.conversation, "reply", "Can you share your platform background?",
        message_id="<their-reply@example.com>")
    person.application.status = ApplicationStatus.rejected
    db.commit()
    with patch("app.services.outreach.compose_message", return_value={"subject": "Wrong new subject",
            "body": "Thanks for replying. My platform work involved APIs.", "generated_by": None}):
        response = outreach.draft_message(db, person, kind="reply")
    with patch.object(outreach_sender.settings, "OUTREACH_CONTACT_COOLDOWN_DAYS", 90), \
         patch("app.services.outreach_sender._deliver") as deliver:
        outreach_sender.send_message(db, response)
    mail = deliver.call_args.args[0]
    assert mail["In-Reply-To"] == "<their-reply@example.com>"
    assert original.message_id in mail["References"] and mail["Subject"] == "Re: Platform role"
    assert response.status == "sent" and response.follow_up_due_at is None
    assert response.conversation.next_action_due_at is None


@pytest.mark.parametrize("kind", ["reply", "thank_you"])
def test_relationship_response_preserves_confirmed_referral_and_custom_action(db, kind):
    person = contact(db)
    original = sent(db, person)
    conversation = original.conversation
    conversation.status, conversation.next_action = "referral_submitted", "Check portal Friday"
    due = conversation.next_action_due_at = networking.now() + timedelta(days=4)
    with patch("app.services.outreach.compose_message", return_value={"subject": "Thanks", "body": "Thank you for the referral.", "generated_by": None}):
        message = outreach.draft_message(db, person, kind=kind)
    outreach.mark_sent(db, message)
    assert conversation.status == "referral_submitted" and conversation.next_action == "Check portal Friday"
    assert conversation.next_action_due_at == due and message.follow_up_due_at is None
    assert original.follow_up_due_at is None


def test_new_role_sequence_retires_old_timer_and_draft(db):
    first, second = contact(db), contact(db)
    old = sent(db, first)
    chase = draft(db, first, "follow_up")
    chase.conversation = old.conversation
    latest = draft(db, second)
    outreach.mark_sent(db, latest)
    assert old.follow_up_due_at is None and chase.status == "skipped"
    assert latest.follow_up_due_at is not None and latest.conversation_id == old.conversation_id


@pytest.mark.parametrize("target", ["draft", "approved"])
def test_sent_history_cannot_be_reopened_for_editing_or_resend(db, target):
    message = sent(db, contact(db))
    with pytest.raises(ValueError, match="cannot become an editable draft"):
        outreach.set_message_status(db, message, target)
    assert message.status == "sent"


@pytest.mark.parametrize("state", ["sending", "uncertain"])
def test_delivery_claim_cannot_be_deleted_or_reset(client, db, state):
    message = draft(db, contact(db))
    message.send_state, message.message_id = state, "<reserved@example.com>"
    message.send_started_at = networking.now()
    db.commit()
    assert client.post(f"/outreach/messages/{message.id}/delete").status_code == 409
    assert client.post(f"/outreach/messages/{message.id}/status", data={"status": "draft"}).status_code == 422
    assert db.get(OutreachMessage, message.id) is not None


def test_bounced_history_stays_read_only_and_cannot_be_deleted(client, db):
    message = sent(db, contact(db))
    outreach.set_message_status(db, message, "bounced")
    assert client.post(f"/outreach/messages/{message.id}/delete").status_code == 409
    assert client.post(f"/outreach/messages/{message.id}/save", data={"body": "altered"}).status_code == 409
    assert message.body == "Earlier platform role message"


@pytest.mark.parametrize("origin", ["manual", "mailbox"])
def test_bounce_invalidates_shared_address_and_replaces_stale_next_action(db, origin):
    first, second = contact(db), contact(db)
    message = sent(db, first)
    pending = draft(db, second, "follow_up")
    conversation = networking.conversation_for(db, second)
    db.commit()
    if origin == "manual":
        outreach.set_message_status(db, message, "bounced")
    else:
        mailbox._record_bounce(db, first.email)
    assert first.email_status == second.email_status == "invalid"
    assert pending.status == "skipped" and message.follow_up_due_at is None
    assert conversation.next_action == "Review bounced address" and conversation.status == "researching"
    assert conversation.next_action_due_at is not None


def test_marking_sent_again_does_not_move_the_sequence_clock(db):
    message = sent(db, contact(db))
    stamp, due = message.sent_at, message.follow_up_due_at
    outreach.set_message_status(db, message, "sent")
    assert message.sent_at == stamp and message.follow_up_due_at == due


def test_regeneration_rechecks_delivery_after_generation_finishes(db):
    message = draft(db, contact(db))
    db.commit()
    original = message.body
    def delivery_started(*args, **kwargs):
        message.send_state, message.message_id = "sending", "<reserved@example.com>"
        message.send_started_at = networking.now()
        db.commit()
        return {"subject": "Replacement", "body": "Must not replace the sent text", "generated_by": None}
    with patch("app.services.outreach.compose_message", side_effect=delivery_started):
        with pytest.raises(ValueError, match="Resolve the delivery"):
            outreach.regenerate_message(db, message)
    assert message.body == original
