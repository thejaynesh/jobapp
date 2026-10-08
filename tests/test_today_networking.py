from datetime import datetime, timedelta, timezone

from app.models.outreach import OutreachMessage
from app.services import daily_plan, networking
from tests.test_outreach import _make_application, _make_contact


def conversation(db):
    app = _make_application(db)
    contact = _make_contact(db, app)
    thread = networking.conversation_for(db, contact)
    db.flush()
    return thread, contact


def test_reply_is_an_action_in_today(db):
    thread, contact = conversation(db)
    thread.status = "reply_needed"
    thread.next_action = "Send the portfolio they requested"
    db.flush()
    plan = daily_plan.build(db, {}, minutes=5)
    assert plan["actions"][0]["title"] == thread.next_action
    assert plan["actions"][0]["kind"] == "reply"
    assert plan["due_count"] == 1


def test_draft_is_reviewable_from_daily_plan(db):
    thread, contact = conversation(db)
    db.add(OutreachMessage(contact_id=contact.id, conversation_id=thread.id,
                          application_id=contact.application_id, status="draft", body="Hello"))
    db.flush()
    plan = daily_plan.build(db, {}, minutes=5)
    assert plan["actions"][0]["kind"] == "review_outreach"
    assert plan["actions"][0]["url"].endswith(str(contact.id))


def test_snoozed_and_do_not_contact_threads_do_not_become_actions(db):
    thread, contact = conversation(db)
    thread.status = "snoozed"
    thread.next_action_due_at = datetime.now(timezone.utc) - timedelta(days=1)
    thread.snoozed_until = datetime.now(timezone.utc) + timedelta(days=1)
    db.flush()
    assert not [a for a in daily_plan.build(db, {}, minutes=5)["actions"] if a["kind"] == "network_followup"]
    thread.snoozed_until = datetime.now(timezone.utc) - timedelta(days=1)
    thread.person.do_not_contact = True
    db.flush()
    assert not [a for a in daily_plan.build(db, {}, minutes=5)["actions"] if a["kind"] == "network_followup"]


def test_saved_networking_estimate_changes_budget(db):
    thread, contact = conversation(db)
    thread.status = "reply_needed"
    db.flush()
    plan = daily_plan.build(db, {"settings": {"plan_networking_minutes": 8}}, minutes=5)
    assert not plan["actions"]
    plan = daily_plan.build(db, {"settings": {"plan_networking_minutes": 8}}, minutes=10)
    assert plan["actions"][0]["minutes"] == 8


def test_one_action_per_conversation_even_with_multiple_drafts(db):
    thread, contact = conversation(db)
    for _ in range(2):
        db.add(OutreachMessage(contact_id=contact.id, conversation_id=thread.id, status="draft", body="Hello"))
    db.flush()
    actions = daily_plan.build(db, {}, minutes=60)["actions"]
    assert len([a for a in actions if a["kind"] == "review_outreach"]) == 1


def test_ready_draft_is_not_an_urgent_followup(db):
    thread, contact = conversation(db)
    thread.status = "ready"
    thread.next_action = "Review draft"
    thread.next_action_due_at = datetime.now(timezone.utc)
    db.flush()
    plan = daily_plan.build(db, {}, minutes=5)
    assert plan["actions"][0]["kind"] == "review_outreach"
    assert plan["actions"][0]["priority"] == 105
    assert plan["due_count"] == 0


def test_relationship_response_remains_actionable_after_application_closed(db):
    from app.models.application import ApplicationStatus
    from app.services.opportunity_actions import build
    thread, contact = conversation(db)
    contact.application.status = ApplicationStatus.rejected
    message = OutreachMessage(contact_id=contact.id, conversation_id=thread.id,
        application_id=contact.application_id, status="draft", kind="initial", body="Hello")
    db.add(message)
    db.flush()
    assert build(db, {}) == []
    for kind in ("reply", "thank_you"):
        message.kind = kind
        db.flush()
        assert build(db, {})[0]["kind"] == "review_outreach"
