from datetime import datetime, timedelta, timezone
import uuid

from app.models.application import Application, ApplicationStatus
from app.models.job import Job, JobStatus
from app.models.intelligence import ApplicationEvent, DecisionEvent
from app.services import application_history as history, tracker


def application(db):
    job = Job(source="test", url="https://example.com/" + uuid.uuid4().hex,
              title="Engineer", company="Example", description="Python", dedupe_hash=uuid.uuid4().hex,
              status=JobStatus.matched, fetched_at=datetime.now(timezone.utc))
    db.add(job)
    db.flush()
    app = Application(job_id=job.id, status=ApplicationStatus.not_applied)
    db.add(app)
    db.flush()
    return app


def test_interview_survives_later_rejection(db):
    app = application(db)
    for status in (ApplicationStatus.applied, ApplicationStatus.interviewing, ApplicationStatus.rejected):
        tracker.set_status(db, app, status)
    db.flush()
    assert "interview_invited" in history.milestones(db, [app.id])[app.id]
    rates = tracker.response_rates(db)
    assert rates["groups"]["Resume"][0]["interviews"] == 1


def test_repeated_status_and_duplicate_receipt_are_idempotent(db):
    app = application(db)
    tracker.set_status(db, app, ApplicationStatus.applied)
    tracker.set_status(db, app, ApplicationStatus.applied)
    history.append(db, app, "receipt", key="same-message")
    history.append(db, app, "receipt", key="same-message")
    assert db.query(ApplicationEvent).count() == 2


def test_decision_features_survive_job_changes(db):
    app = application(db)
    app.job.llm_score = 85
    history.record_decision(db, app.job, {}, "yes")
    app.job.llm_score = 30
    snapshot = db.query(DecisionEvent).one().payload
    assert snapshot["score"] == 85 and snapshot["features"]["score"] == .85


def test_late_confirmed_receipt_does_not_reopen_a_rejection(db):
    app = application(db)
    now = datetime.now(timezone.utc)
    tracker.set_status(db, app, ApplicationStatus.rejected, now=now)
    event = history.append(db, app, "mail_suggestion", payload={"milestone": "receipt"}, when=now - timedelta(days=5))
    history.review_mail(db, event, True, {})
    history.review_mail(db, event, True, {})
    assert app.status == ApplicationStatus.rejected
    assert db.query(ApplicationEvent).filter(ApplicationEvent.kind == "receipt").count() == 1
