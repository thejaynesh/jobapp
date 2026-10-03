from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import delete, text

from app.models.application import Application, ApplicationDocument, ApplicationStatus, DocType
from app.models.intelligence import ApplicationEvent, DecisionEvent
from app.models.job import Job
from app.models.profile import Profile
from app.services import application_history as history, mailbox, tracker
from tests.test_application_history import application


def test_fetch_state_and_manual_trigger_work_during_capacity_pause(client, monkeypatch):
    from app.services import capacity, fetch_lock
    from app.tasks import fetch

    monkeypatch.setattr(capacity, "allow_background", lambda *_: False)
    monkeypatch.setattr(fetch_lock, "any_state", lambda *_: {"running": True, "seconds_left": 90})
    assert fetch.fetch_state() == {"running": True, "seconds_left": 90}
    response = client.get("/runs/status")
    assert response.status_code == 200 and "Fetch running" in response.text
    assert 'hx-trigger="every 5s"' in response.text
    monkeypatch.setattr(fetch_lock, "any_state", lambda *_: {"running": False, "seconds_left": None})
    send = Mock()
    monkeypatch.setattr(fetch.fetch_jobs, "delay", send)
    response = client.post("/runs/trigger", data={"group": "api"})
    assert response.status_code == 200
    send.assert_called_once_with(only=None, match_after=True, group="api")


def retract(db, app, event):
    db.refresh(app, with_for_update=True)
    correction = history.append(db, app, "correction", payload={"supersedes": str(event.id), "note": "Mistaken event"})
    history.project_status(db, app, {})
    db.commit()
    return correction


def test_corrected_status_keeps_its_date_and_accepts_later_historical_mail(db):
    app = application(db)
    now = datetime.now(timezone.utc)
    for status, days in [(ApplicationStatus.applied, 20), (ApplicationStatus.interviewing, 10),
                         (ApplicationStatus.rejected, 1)]:
        tracker.set_status(db, app, status, now=now - timedelta(days=days))
    db.commit()
    rejected = next(e for e in history.timeline(db, app.id) if e.kind == "rejected")
    retract(db, app, rejected)
    assert app.status_changed_at == now - timedelta(days=10)
    assert app.next_action_due == (now - timedelta(days=9)).date()
    offer = history.append(db, app, "mail_suggestion", when=now - timedelta(days=5), payload={"milestone": "offered"})
    history.review_mail(db, offer, True, {})
    db.commit()
    assert app.status == ApplicationStatus.offered
    assert app.status_changed_at == offer.occurred_at


def test_reprojection_updates_a_same_status_date_without_replacing_custom_action(db):
    app = application(db)
    first = datetime.now(timezone.utc) - timedelta(days=20)
    tracker.set_status(db, app, ApplicationStatus.applied, now=first)
    newer = history.record_milestone(db, app, "receipt", {}, when=first + timedelta(days=4))
    history.project_status(db, app, {})
    app.next_action, app.next_action_due = "My own next step", first.date()
    db.commit()
    retract(db, app, newer)
    assert app.status == ApplicationStatus.applied
    assert app.status_changed_at == first
    assert (app.next_action, app.next_action_due) == ("My own next step", first.date())


def test_submission_correction_undo_and_reapply_preserve_the_right_snapshot(db):
    app = application(db)
    old_date = datetime.now(timezone.utc) - timedelta(days=40)
    first = ApplicationDocument(application_id=app.id, doc_type=DocType.resume, version=1,
                                path="/tmp/first.pdf", is_current=True)
    letter = ApplicationDocument(application_id=app.id, doc_type=DocType.cover_letter, version=1,
                                 path="/tmp/letter.pdf", is_current=True)
    db.add_all([first, letter]); db.commit()
    app.job.llm_score = 80
    tracker.set_status(db, app, ApplicationStatus.applied, now=old_date)
    history.append(db, app, "submitted_document", when=old_date + timedelta(days=1),
                   payload={"document_id": str(first.id)})
    db.commit()
    original = next(e for e in history.timeline(db, app.id) if e.kind == "applied")
    original_decision = history.application_decisions(db, [app])[app.id]
    correction = retract(db, app, original)
    assert app.status == ApplicationStatus.not_applied
    assert app.applied_at is None and app.sent_resume_id is None and app.sent_cover_letter is None

    first.is_current = letter.is_current = False
    second = ApplicationDocument(application_id=app.id, doc_type=DocType.resume, version=2,
                                 path="/tmp/second.pdf", is_current=True)
    db.add(second); db.commit()
    retract(db, app, correction)
    assert app.applied_at == old_date and app.sent_resume_id == first.id and app.sent_cover_letter is True
    assert history.application_decisions(db, [app])[app.id].id == original_decision.id

    retract(db, app, original)
    app.job.llm_score = 25
    applied_today = datetime.now(timezone.utc)
    tracker.set_status(db, app, ApplicationStatus.applied, now=applied_today)
    history.project_status(db, app, {})
    db.commit()
    assert app.applied_at == applied_today and app.sent_resume_id == second.id and app.sent_cover_letter is False
    active = history.application_decisions(db, [app])[app.id]
    assert active.id != original_decision.id and active.payload["score"] == 25
    assert db.query(DecisionEvent).filter(DecisionEvent.kind == "yes").count() == 2


def test_explicit_reset_starts_a_new_cycle_and_undo_restores_the_old_one(db):
    app = application(db)
    original_date = datetime.now(timezone.utc) - timedelta(days=30)
    tracker.set_status(db, app, ApplicationStatus.applied, now=original_date)
    db.commit()
    original = history.application_decisions(db, [app])[app.id]
    tracker.set_status(db, app, ApplicationStatus.not_applied)
    db.commit()
    reset = next(e for e in history.timeline(db, app.id) if e.kind == "reset")
    assert app.applied_at is None
    retract(db, app, reset)
    assert app.applied_at == original_date
    assert history.application_decisions(db, [app])[app.id].id == original.id


def test_reapplying_does_not_attribute_an_old_interview_or_channel_to_the_new_resume(db):
    from app.services import outcome_learning

    app = application(db)
    now = datetime.now(timezone.utc)
    tracker.set_status(db, app, ApplicationStatus.applied, now=now - timedelta(days=90))
    history.append(db, app, "application_channel", when=now - timedelta(days=89), payload={"channel": "referral"})
    tracker.set_status(db, app, ApplicationStatus.interviewing, now=now - timedelta(days=80))
    tracker.set_status(db, app, ApplicationStatus.not_applied, now=now - timedelta(days=70))
    app.job.llm_score = 25
    tracker.set_status(db, app, ApplicationStatus.applied, now=now - timedelta(days=40))
    db.commit()

    assert "interview_invited" not in history.milestones(db, [app.id])[app.id]
    assert app.id not in history.channels(db, [app.id])
    cohort = outcome_learning.cohort(db, {}, now)
    assert cohort["confirmed"] == 0 and cohort["unknown"] == 1
    assert cohort["channels"]["not recorded"]["interviews"] == 0
    assert tracker.response_rates(db)["groups"]["Resume"][0]["interviews"] == 0

    tracker.set_status(db, app, ApplicationStatus.rejected, now=now - timedelta(days=2))
    db.commit()
    row = outcome_learning.cohort(db, {}, now)["rows"][0]
    assert row["outcome"] == 0 and row["score"] == 25


def test_first_post_upgrade_transition_preserves_known_submission_metadata(db):
    app = application(db)
    resume = ApplicationDocument(application_id=app.id, doc_type=DocType.resume, version=1,
                                 path="/tmp/legacy.pdf", is_current=True)
    db.add(resume); db.flush()
    app.status = ApplicationStatus.interviewing
    app.applied_at = datetime.now(timezone.utc) - timedelta(days=30)
    app.status_changed_at = app.applied_at + timedelta(days=10)
    app.sent_resume_id, app.sent_cover_letter = resume.id, True
    expected = app.applied_at, app.status_changed_at
    history.record_decision(db, app.job, {}, "yes", origin="application", key=f"application:{app.id}:decision")
    db.commit()
    tracker.set_status(db, app, ApplicationStatus.rejected)
    db.commit()
    rejected = next(e for e in history.timeline(db, app.id) if e.kind == "rejected")
    retract(db, app, rejected)
    assert (app.applied_at, app.status_changed_at) == expected
    assert app.sent_resume_id == resume.id and app.sent_cover_letter is True
    assert app.id in history.application_decisions(db, [app])


def test_concurrent_milestone_requests_do_not_deadlock(monkeypatch):
    from app.routers import intelligence
    from tests.conftest import TestSessionLocal

    with TestSessionLocal() as db:
        app = application(db)
        tracker.set_status(db, app, ApplicationStatus.applied)
        db.commit()
        app_id, job_id = app.id, app.job_id
    monkeypatch.setattr(intelligence, "get_or_create_profile", lambda _: SimpleNamespace(data={}))
    inserted = threading.Event()
    guard, count = threading.Lock(), 0
    append = history.append

    def overlap(*args, **kwargs):
        nonlocal count
        result = append(*args, **kwargs)
        with guard:
            count += 1
            if count == 2:
                inserted.set()
        # Previously both INSERTs acquired KEY SHARE, then deadlocked while
        # upgrading to UPDATE. With the fix the second insert waits until the
        # first request commits, so only the first wait reaches its timeout.
        inserted.wait(timeout=0.5)
        return result

    monkeypatch.setattr(history, "append", overlap)

    def record(kind):
        with TestSessionLocal() as db:
            db.execute(text("SET LOCAL statement_timeout = '5s'"))
            return intelligence.milestone(app_id, kind=kind, note="", db=db).status_code

    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            assert list(workers.map(record, ["assessment", "interview_invited"])) == [303, 303]
        with TestSessionLocal() as db:
            assert {e.kind for e in history.timeline(db, app_id)} >= {"assessment", "interview_invited"}
    finally:
        with TestSessionLocal() as db:
            db.execute(delete(Job).where(Job.id == job_id)); db.commit()


class MailClient:
    def __init__(self, uids, fail_once=None):
        self.uids, self.fail_once, self.fetched = uids, fail_once, []

    def select(self, *args, **kwargs): return "OK", []
    def status(self, *args): return "OK", [b"INBOX (UIDVALIDITY 1)"]
    def logout(self): pass

    def uid(self, action, *args):
        if action == "SEARCH":
            return "OK", [" ".join(map(str, self.uids)).encode()]
        uid = int(args[0])
        self.fetched.append(uid)
        if uid == self.fail_once:
            self.fail_once = None
            return "NO", []
        mail = EmailMessage()
        mail["Subject"] = str(uid)
        mail.set_content("Test message")
        return "OK", [(b"", mail.as_bytes())]


def mailbox_setup(db, monkeypatch, client):
    profile = Profile(data={"mailbox": {"uidvalidity": "1", "last_uid": 1}})
    db.add(profile); db.commit()
    monkeypatch.setattr(mailbox, "mailbox_blocked_reason", lambda: "")
    monkeypatch.setattr(mailbox, "_connect", lambda: client)
    return profile


def test_mailbox_drains_all_pending_messages_in_bounded_oldest_first_batches(db, monkeypatch):
    client = MailClient([5, 2, 1, 4, 3])
    profile = mailbox_setup(db, monkeypatch, client)
    processed = []
    monkeypatch.setattr(mailbox, "_process", lambda db, mail, counts: processed.append(int(mail["Subject"])))
    mailbox.poll(db, limit=2)
    assert profile.data["mailbox"]["last_uid"] == 3
    mailbox.poll(db, limit=2)
    mailbox.poll(db, limit=2)
    assert processed == client.fetched == [2, 3, 4, 5]
    assert profile.data["mailbox"]["last_uid"] == 5


def test_failed_mail_fetch_does_not_advance_past_an_unprocessed_uid(db, monkeypatch):
    client = MailClient([2, 3, 4], fail_once=3)
    profile = mailbox_setup(db, monkeypatch, client)
    processed = []
    monkeypatch.setattr(mailbox, "_process", lambda db, mail, counts: processed.append(int(mail["Subject"])))
    mailbox.poll(db, limit=3)
    assert processed == [2] and profile.data["mailbox"]["last_uid"] == 2
    mailbox.poll(db, limit=3)
    assert processed == [2, 3, 4] and profile.data["mailbox"]["last_uid"] == 4


def test_failed_mail_processing_retries_before_later_messages(db, monkeypatch):
    client = MailClient([2, 3, 4])
    profile = mailbox_setup(db, monkeypatch, client)
    processed, failures = [], [3]

    def process(db, mail, counts):
        uid = int(mail["Subject"])
        if uid in failures:
            failures.remove(uid)
            raise RuntimeError("Temporary processing failure")
        processed.append(uid)

    monkeypatch.setattr(mailbox, "_process", process)
    mailbox.poll(db, limit=3)
    assert processed == [2] and profile.data["mailbox"]["last_uid"] == 2
    mailbox.poll(db, limit=3)
    assert processed == [2, 3, 4] and profile.data["mailbox"]["last_uid"] == 4


@pytest.mark.parametrize("header", [b"Content-Type", b"References", b"In-Reply-To",
                                   b"Auto-Submitted", b"X-Autoreply", b"X-Autorespond"])
def test_malformed_mail_header_does_not_block_later_messages(db, monkeypatch, header):
    class RawMailClient(MailClient):
        def uid(self, action, *args):
            if action == "FETCH" and int(args[0]) == 2:
                self.fetched.append(2)
                raw = (b"From: recruiter@example.com\r\n" + header
                       + b": invalid-\xff\r\n\r\nHello")
                return "OK", [(b"", raw)]
            return super().uid(action, *args)

    client = RawMailClient([2, 3])
    profile = mailbox_setup(db, monkeypatch, client)
    # Exercise the real parser and processor; only IMAP is replaced.
    counts = mailbox.poll(db, limit=2)
    assert counts["scanned"] == 2
    assert client.fetched == [2, 3]
    assert profile.data["mailbox"]["last_uid"] == 3


@pytest.mark.parametrize("field", ["subject", "body", "message_id"])
def test_mail_nul_characters_are_normalized_before_storing_suggestions(db, monkeypatch, field):
    app = application(db)
    tracker.set_status(db, app, ApplicationStatus.applied)
    db.commit()
    content = {"subject": b"Engineer at Example", "message_id": b"<receipt@example.com>",
               "body": b"Thank you for applying: " + app.job.url.encode()}
    content[field] += b"\x00"
    raw = (b"From: recruiter@example.com\r\nSubject: " + content["subject"]
           + b"\r\nMessage-ID: " + content["message_id"]
           + b"\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n" + content["body"])

    class RawMailClient(MailClient):
        def uid(self, action, *args):
            if action == "FETCH" and int(args[0]) == 2:
                self.fetched.append(2)
                return "OK", [(b"", raw)]
            return super().uid(action, *args)

    client = RawMailClient([2, 3])
    profile = mailbox_setup(db, monkeypatch, client)
    counts = mailbox.poll(db, limit=2)
    assert counts["scanned"] == 2 and profile.data["mailbox"]["last_uid"] == 3
    suggestion = db.query(ApplicationEvent).filter(ApplicationEvent.kind == "mail_suggestion").one()
    assert suggestion.application_id == app.id
    assert "\x00" not in suggestion.payload["subject"] + suggestion.payload["snippet"] + suggestion.payload["message_id"]
