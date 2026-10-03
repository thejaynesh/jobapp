"""Append-only milestones, reviewable mail suggestions and decision snapshots."""
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.dialects.postgresql import insert

from app.models.intelligence import ApplicationEvent, DecisionEvent
from app.services.evidence import fingerprint

MILESTONES = {"applied", "receipt", "assessment", "interview_invited", "interview_completed",
              "offered", "rejected", "withdrawn"}
STATUS_KIND = {"interviewing": "interview_invited", "not_applied": "reset"}
SENT_KINDS = MILESTONES - {"withdrawn"}


def utcnow():
    return datetime.now(timezone.utc)


def append(db, application, kind, *, origin="user", payload=None, when=None, key=None):
    if application.id is None:
        db.flush()
    key = key or str(uuid.uuid4())
    event_id = uuid.uuid4()
    stmt = insert(ApplicationEvent).values(
        id=event_id, application_id=application.id, kind=kind, origin=origin,
        dedupe_key=key[:160], occurred_at=when or utcnow(), payload=payload or {},
    ).on_conflict_do_nothing(index_elements=[ApplicationEvent.dedupe_key]).returning(ApplicationEvent.id)
    stored = db.execute(stmt).scalar_one_or_none()
    return db.get(ApplicationEvent, stored) if stored else None


def timeline(db, application_id):
    return db.query(ApplicationEvent).filter(ApplicationEvent.application_id == application_id).order_by(
        ApplicationEvent.occurred_at.desc(), ApplicationEvent.observed_at.desc()).all()


def corrected_ids(events):
    # Corrections only reference an already existing event. Walk newest first
    # so retracting a correction restores the original event without erasing it.
    removed = set()
    ordered = sorted(events, key=lambda e: (e.observed_at or e.occurred_at, e.occurred_at), reverse=True)
    for event in ordered:
        if event.kind == "correction" and str(event.id) not in removed:
            removed.add(str(event.payload.get("supersedes")))
    return removed


def effective(events):
    removed = corrected_ids(events)
    return [e for e in events if str(e.id) not in removed and e.kind != "correction"]


def milestones(db, application_ids) -> dict:
    rows = db.query(ApplicationEvent).filter(ApplicationEvent.application_id.in_(application_ids)).order_by(
        ApplicationEvent.occurred_at.desc(), ApplicationEvent.observed_at.desc()).all() if application_ids else []
    groups = {}
    for row in rows:
        groups.setdefault(row.application_id, []).append(row)
    return {key: {e.kind for e in _cycle(events)} for key, events in groups.items()}


def channels(db, application_ids):
    groups = {}
    if application_ids:
        for event in db.query(ApplicationEvent).filter(ApplicationEvent.application_id.in_(application_ids)).order_by(
                ApplicationEvent.occurred_at.desc(), ApplicationEvent.observed_at.desc()):
            groups.setdefault(event.application_id, []).append(event)
    found = {}
    for application_id, events in groups.items():
        channel = next((e for e in _cycle(events) if e.kind == "application_channel"), None)
        if channel:
            found[application_id] = channel.payload.get("channel", "not recorded")
    return found


def record_decision(db, job, profile, kind, *, origin="jobs", position=None, now=None, key=None, batch=None, profile_hash=None):
    from app.services.tunables import value
    if not value(profile, "record_decisions_enabled"):
        return
    from app.services.for_you import features
    from app.services.experience import total_years
    if kind not in {"yes", "no", "shown", "reset"}:
        raise ValueError("Unsupported decision")
    now = now or utcnow()
    if job.id is None:
        db.flush()
    from app.services import evidence
    data = {
        "features": features(job, total_years(profile.get("experience") or []), now),
        "score": job.llm_score_deep if job.llm_score_deep is not None else job.llm_score,
        "profile_hash": profile_hash or fingerprint(evidence.facts(profile)),
        "posting_hash": evidence.posting_hash(job),
        "family": fingerprint([str(job.company or "").casefold(), str(job.title or "").casefold()]),
        "company": job.company, "title": job.title, "origin": origin, "position": position,
        "ranking_version": (profile.get("ranking_model") or {}).get("trained_at"),
    }
    # Repeated page refreshes within an hour are one exposure observation.
    dedupe = key or (f"shown:{job.id}:{origin}:{now.strftime('%Y%m%d%H')}" if kind == "shown" else str(uuid.uuid4()))
    row = dict(id=uuid.uuid4(), job_id=job.id, kind=kind, dedupe_key=dedupe, occurred_at=now, payload=data)
    if batch is not None:
        batch.append(row)
    else:
        db.execute(insert(DecisionEvent).values(**row).on_conflict_do_nothing(index_elements=[DecisionEvent.dedupe_key]))


def impressions(db, jobs, profile, origin="jobs"):
    """One INSERT for the visible page, and one profile fingerprint."""
    from app.services import evidence
    digest, batch, now = fingerprint(evidence.facts(profile)), [], utcnow()
    for position, job in enumerate(jobs):
        record_decision(db, job, profile, "shown", origin=origin, position=position, now=now, batch=batch, profile_hash=digest)
    if batch:
        db.execute(insert(DecisionEvent).values(batch).on_conflict_do_nothing(index_elements=[DecisionEvent.dedupe_key]))


def _submission(application, when):
    from app.models.application import DocType

    resume = next((d for d in application.documents if d.doc_type == DocType.resume and d.is_current), None)
    resume_id = application.sent_resume_id or (resume.id if resume and not application.applied_at else None)
    return {"applied_at": (application.applied_at or when).isoformat(),
            "resume_id": str(resume_id) if resume_id else None,
            "cover_letter": application.sent_cover_letter if application.applied_at or application.sent_cover_letter is not None else
                any(d.doc_type == DocType.cover_letter and d.is_current for d in application.documents)}


def record_milestone(db, application, kind, profile, *, when=None, payload=None, origin="user", key=None):
    """Record submission evidence once per application cycle, preserving old cycles."""
    if application.id is None:
        db.flush()
    when = when or utcnow()
    previous = timeline(db, application.id)
    # Capture the known prior status on the first transition after upgrade so
    # an existing interview does not vanish when the user records rejection.
    old = application.status.value if application.status else "not_applied"
    if not any(e.kind in MILESTONES | {"reset"} for e in previous) and old != "not_applied":
        legacy = {"inferred": True, "note": "Known status when history recording began"}
        if application.applied_at:
            legacy["submission"] = {**_submission(application, when),
                "decision_key": f"application:{application.id}:decision"}
        append(db, application, STATUS_KIND.get(old, old), origin="legacy",
               when=application.status_changed_at or application.applied_at or when,
               payload=legacy)
    job = application.job
    payload = dict(payload or {})
    decision_key = None
    if kind in SENT_KINDS and not application.applied_at:
        decision_key = f"application:{application.id}:decision:{uuid.uuid4()}"
        payload.update({"posting": {"title": job.title, "company": job.company,
            "url": job.url, "description": (job.description or "")[:50000]},
            "profile_hash": fingerprint(profile), "document_confirmation": "inferred",
            "submission": {**_submission(application, when), "decision_key": decision_key}})
        # Keep the original field readable for existing history consumers.
        payload["resume_id"] = payload["submission"]["resume_id"]
    event = append(db, application, kind, payload=payload, when=when, origin=origin, key=key)
    if event and decision_key:
        record_decision(db, job, profile, "yes", origin="application", now=when, key=decision_key)
    return event


def record_status(db, application, status, profile, when):
    old = application.status.value if application.status else "not_applied"
    return record_milestone(db, application, STATUS_KIND.get(status.value, status.value), profile,
        when=when, payload={"status": status.value, "previous": old})


def _cycle(events):
    """Effective events in the latest cycle; undoing a reset restores its predecessor."""
    result = []
    for event in effective(events):
        if event.kind == "reset":
            break
        result.append(event)
    return result


def application_decisions(db, applications):
    """Choose the snapshot belonging to surviving submission evidence, not a retracted apply."""
    grouped = {}
    if applications:
        rows = db.query(ApplicationEvent).filter(ApplicationEvent.application_id.in_(
            [application.id for application in applications])).order_by(
                ApplicationEvent.occurred_at.desc(), ApplicationEvent.observed_at.desc()).all()
        for event in rows:
            grouped.setdefault(event.application_id, []).append(event)
    keys = {}
    for application in applications:
        events = _cycle(grouped.get(application.id, []))
        submissions = [e for e in reversed(events) if (e.payload or {}).get("submission")]
        if submissions:
            keys[application.id] = submissions[0].payload["submission"].get("decision_key")
        elif not events or any(e.kind in SENT_KINDS for e in events):
            keys[application.id] = f"application:{application.id}:decision"
    rows = {row.dedupe_key: row for row in db.query(DecisionEvent).filter(
        DecisionEvent.dedupe_key.in_([key for key in keys.values() if key])).all()} if keys else {}
    return {application_id: rows[key] for application_id, key in keys.items() if key in rows}


def pending(db, limit=100):
    from sqlalchemy import String, cast, exists
    from sqlalchemy.orm import aliased
    review = aliased(ApplicationEvent)
    return db.query(ApplicationEvent).filter(ApplicationEvent.kind == "mail_suggestion",
        ~exists().where(review.dedupe_key == "review:" + cast(ApplicationEvent.id, String))).order_by(
        ApplicationEvent.observed_at.desc()).limit(limit).all()


def review_mail(db, event, accept, profile):
    from app.models.application import Application, ApplicationStatus
    from app.services import tracker
    if event.kind != "mail_suggestion":
        raise ValueError("Not an application-mail suggestion")
    application = db.get(Application, event.application_id)
    db.flush()
    db.refresh(application, attribute_names=["status", "status_changed_at", "applied_at"], with_for_update=True)
    review = append(db, application, "mail_review", key="review:" + str(event.id),
                    payload={"suggestion_id": str(event.id), "accepted": bool(accept)})
    if not review or not accept:
        return
    kind = event.payload.get("milestone")
    if kind not in MILESTONES:
        raise ValueError("Unknown milestone")
    record_milestone(db, application, kind, profile, origin="mail_confirmed", when=event.occurred_at,
        key="confirmed:" + str(event.id), payload={"suggestion_id": str(event.id), "message_id": event.payload.get("message_id")})
    mapping = {"receipt": "applied", "assessment": "applied", "interview_invited": "interviewing",
               "interview_completed": "interviewing", "offered": "offered", "rejected": "rejected"}
    # Late receipts must not move a completed application backwards.
    if kind in mapping and (not application.status_changed_at or event.occurred_at >= application.status_changed_at):
        tracker.set_status(db, application, ApplicationStatus(mapping[kind]), now=event.occurred_at, profile_data=profile, record_event=False)


def project_status(db, application, profile):
    """Rebuild the board projection after the user retracts a mistaken event."""
    from app.models.application import Application, ApplicationDocument, ApplicationStatus, DocType
    from app.services import tracker
    db.query(Application.id).filter(Application.id == application.id).with_for_update().one()
    mapping = {"applied": "applied", "receipt": "applied", "assessment": "applied",
               "interview_invited": "interviewing", "interview_completed": "interviewing",
               "offered": "offered", "rejected": "rejected", "withdrawn": "withdrawn", "reset": "not_applied"}
    all_events = timeline(db, application.id)
    events = [e for e in effective(all_events) if e.kind in mapping]
    latest = events[0] if events else None
    tracker.set_status(db, application, ApplicationStatus(mapping[latest.kind] if latest else "not_applied"),
                       now=latest.occurred_at if latest else application.created_at,
                       profile_data=profile, record_event=False, reproject=True)
    application.status_changed_at = latest.occurred_at if latest else None
    cycle = _cycle(all_events)
    sent = [e for e in reversed(cycle) if e.kind in SENT_KINDS or e.payload.get("submission")]
    if not sent:
        application.applied_at = None
        application.sent_resume_id = None
        application.sent_cover_letter = None
        return
    first = next((e for e in sent if e.payload.get("submission") or "resume_id" in e.payload), sent[0])
    snapshot = first.payload.get("submission") or {}
    application.applied_at = datetime.fromisoformat(snapshot["applied_at"]) if snapshot.get("applied_at") else first.occurred_at
    # New events preserve submission metadata for undo. Older events have only
    # resume_id; absent evidence remains unknown, never today's current resume.
    resume_id = snapshot.get("resume_id", first.payload.get("resume_id"))
    # A later reapplication has its own snapshot. An older confirmation belongs
    # to the retracted submission and must not replace the newly sent version.
    confirmed = next((e for e in cycle if e.kind == "submitted_document"
                      and e.occurred_at >= first.occurred_at), None)
    if confirmed:
        resume_id = confirmed.payload.get("document_id")
    try:
        resume = db.get(ApplicationDocument, uuid.UUID(resume_id)) if resume_id else None
    except (ValueError, TypeError):
        resume = None
    application.sent_resume_id = resume.id if resume and resume.application_id == application.id and resume.doc_type == DocType.resume else None
    application.sent_cover_letter = snapshot.get("cover_letter")


def prune(db, profile):
    from app.services.tunables import value
    before = utcnow() - timedelta(days=int(value(profile, "decision_retention_days")))
    return db.query(DecisionEvent).filter(DecisionEvent.occurred_at < before).delete(synchronize_session=False)
