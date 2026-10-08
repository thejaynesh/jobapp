"""Daily actions, evidence inspection, history and reviewed corrections."""
import copy
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.application import Application, ApplicationDocument, DocType
from app.models.intelligence import ApplicationEvent, DecisionEvent
from app.models.job import Job
from app.services import application_history as history, daily_plan, evidence
from app.services.profile_service import get_or_create_profile
from app.templating import build

router = APIRouter(tags=["intelligence"])
templates = build()


@router.get("/today", response_class=HTMLResponse)
def today(request: Request, minutes: int | None = None, db: Session = Depends(get_db)):
    profile = get_or_create_profile(db).data or {}
    plan = daily_plan.build(db, profile, minutes)
    history.impressions(db, [a["job"] for a in plan["actions"] if a["job"]], profile, "today")
    from app.services import capacity, outcome_learning
    response = templates.TemplateResponse("intelligence/today.html", {"request": request,
        "plan": plan, "mail": history.pending(db), "capacity": capacity.status(profile),
        "outcomes": outcome_learning.report(db, profile), "clarifications": profile.get("requirement_answers") or {},
        "semantic_report": profile.get("intelligence_report") or {}})
    db.commit()
    return response


@router.get("/jobs/{job_id}/evidence", response_class=HTMLResponse)
def job_evidence(job_id: uuid.UUID, request: Request, db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    profile = get_or_create_profile(db).data or {}
    return templates.TemplateResponse("intelligence/evidence.html", {"request": request, "job": job,
        "assessment": evidence.current(job, profile)})


@router.post("/today/answer")
def answer(question_id: str = Form(...), answer: str = Form(...), question_kind: str = Form("skill"),
           satisfaction: str = Form("unsure"), job_id: uuid.UUID | None = Form(None),
           db: Session = Depends(get_db)):
    if not question_id.startswith("r-") or len(question_id) != 22 or not 1 <= len(answer.strip()) <= 1600:
        raise HTTPException(422, "A short factual answer is required")
    if job_id is not None:
        job = db.get(Job, job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        requirement = next((row for row in evidence.requirements(job) if row["id"] == question_id), None)
        if requirement is None:
            raise HTTPException(422, "This requirement has changed; review the posting again")
        question_kind = requirement.get("kind", "skill")
    if question_kind not in evidence.ANSWER_KINDS or satisfaction not in evidence.SATISFACTIONS:
        raise HTTPException(422, "Choose whether you meet the requirement")
    profile = get_or_create_profile(db)
    data = copy.deepcopy(profile.data or {})
    answers = data.setdefault("requirement_answers", {})
    if len(answers) >= 500 and question_id not in answers:
        raise HTTPException(422, "Remove an old clarification before adding another")
    now = datetime.now(timezone.utc)
    answers[question_id] = {"text": answer.strip(), "at": now.isoformat(),
                            "kind": question_kind, "satisfaction": satisfaction}
    if question_kind == "eligibility":
        from app.services.tunables import value
        answers[question_id]["expires_at"] = (now + timedelta(days=value(data, "answer_expiry_days"))).isoformat()
    profile.data = data
    db.commit()
    return RedirectResponse("/today", 303)


@router.post("/today/pins")
def pins(companies: str = Form(""), db: Session = Depends(get_db)):
    profile = get_or_create_profile(db)
    data = copy.deepcopy(profile.data or {})
    data["plan_pins"] = list(dict.fromkeys(line.strip()[:120] for line in companies.splitlines() if line.strip()))[:50]
    from app.services import company_identity
    watched = company_identity.sync_pins(db, data["plan_pins"])
    profile.data = data
    db.commit()
    from app.routers.companies import _queue
    for company in watched:
        _queue(company.id)
    return RedirectResponse("/today", 303)


@router.post("/today/dismiss/{job_id}")
def dismiss_suggestion(job_id: uuid.UUID, reason: str = Form("other"), db: Session = Depends(get_db)):
    from app.models.job import JobStatus
    from app.services.match_report import DISMISS_REASONS
    if reason not in DISMISS_REASONS:
        raise HTTPException(422, "Choose a dismissal reason")
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    history.record_decision(db, job, get_or_create_profile(db).data or {}, "no", origin="today")
    job.status, job.filter_reason = JobStatus.filtered_out, "manual"
    job.favourite = False
    job.favourited_at = None
    job.dismiss_reason, job.dismissed_at = reason, datetime.now(timezone.utc)
    job.filter_detail = "You dismissed this suggestion in Today."
    db.commit()
    return RedirectResponse("/today", 303)


@router.post("/today/answers/{question_id}/delete")
def delete_answer(question_id: str, db: Session = Depends(get_db)):
    profile = get_or_create_profile(db)
    data = copy.deepcopy(profile.data or {})
    data["requirement_answers"] = {k: v for k, v in (data.get("requirement_answers") or {}).items() if k != question_id}
    profile.data = data
    db.commit()
    return RedirectResponse("/today", 303)


@router.post("/today/mail/{event_id}")
def review_mail(event_id: uuid.UUID, accept: bool = Form(False), db: Session = Depends(get_db)):
    event = db.get(ApplicationEvent, event_id)
    if not event or event.kind != "mail_suggestion":
        raise HTTPException(404, "Suggestion not found")
    history.review_mail(db, event, accept, get_or_create_profile(db).data or {})
    db.commit()
    return RedirectResponse("/today", 303)


@router.post("/apps/{application_id}/milestone")
def milestone(application_id: uuid.UUID, kind: str = Form(...), note: str = Form(""), db: Session = Depends(get_db)):
    application = db.get(Application, application_id)
    if not application:
        raise HTTPException(404, "Application not found")
    if kind not in history.MILESTONES:
        raise HTTPException(422, "Unknown milestone")
    # Take the exclusive lock before the event INSERT's foreign-key check
    # acquires KEY SHARE; concurrent inserts cannot both upgrade that lock.
    db.refresh(application, with_for_update=True)
    profile = get_or_create_profile(db).data or {}
    history.record_milestone(db, application, kind, profile, payload={"note": note[:800]})
    history.project_status(db, application, profile)
    db.commit()
    return RedirectResponse(f"/apps/{application_id}", 303)


@router.post("/apps/{application_id}/events/{event_id}/correct")
def correct(application_id: uuid.UUID, event_id: uuid.UUID, note: str = Form(...), db: Session = Depends(get_db)):
    event = db.get(ApplicationEvent, event_id)
    if not event or event.application_id != application_id or not note.strip():
        raise HTTPException(422, "Choose an event and explain the correction")
    application = db.get(Application, application_id)
    db.refresh(application, with_for_update=True)
    # Repeated submissions are a no-op; after an undo the event can be
    # retracted again without colliding with the original correction.
    if str(event_id) not in history.corrected_ids(history.timeline(db, application_id)):
        history.append(db, application, "correction", payload={"supersedes": str(event_id), "note": note[:800]})
        history.project_status(db, application, get_or_create_profile(db).data or {})
    db.commit()
    return RedirectResponse(f"/apps/{application_id}", 303)


@router.post("/apps/{application_id}/submitted-document")
def submitted_document(application_id: uuid.UUID, document_id: uuid.UUID = Form(...), db: Session = Depends(get_db)):
    application = db.get(Application, application_id)
    document = db.get(ApplicationDocument, document_id)
    if not application or not document or document.application_id != application_id or document.doc_type != DocType.resume:
        raise HTTPException(422, "Choose a resume belonging to this application")
    application.sent_resume_id = document.id
    history.append(db, application, "submitted_document", payload={"document_id": str(document.id),
        "version": document.version, "content_hash": evidence.fingerprint(document.content), "confirmation": "user"})
    db.commit()
    return RedirectResponse(f"/apps/{application_id}", 303)


@router.post("/apps/{application_id}/channel")
def application_channel(application_id: uuid.UUID, channel: str = Form(...), db: Session = Depends(get_db)):
    if channel not in {"cold application", "referral", "outreach", "not recorded"}:
        raise HTTPException(422, "Choose an application channel")
    application = db.get(Application, application_id)
    if not application:
        raise HTTPException(404, "Application not found")
    history.append(db, application, "application_channel", payload={"channel": channel})
    db.commit()
    return RedirectResponse(f"/apps/{application_id}", 303)


@router.get("/intelligence/export")
def export_history(db: Session = Depends(get_db)):
    def serialize(event):
        return {"id": str(event.id), "kind": event.kind, "at": event.occurred_at.isoformat(),
                "payload": event.payload, "job_id": str(getattr(event, "job_id", "")),
                "application_id": str(getattr(event, "application_id", ""))}
    return JSONResponse({"version": 1, "limit_per_collection": 50000,
        "clarifications": (get_or_create_profile(db).data or {}).get("requirement_answers") or {},
        "applications": [serialize(e) for e in db.query(ApplicationEvent).order_by(ApplicationEvent.occurred_at).limit(50000)],
        "decisions": [serialize(e) for e in db.query(DecisionEvent).order_by(DecisionEvent.occurred_at).limit(50000)]},
        headers={"Content-Disposition": 'attachment; filename="jobapp-history.json"'})


@router.post("/intelligence/decisions/delete")
def delete_decisions(confirm: str = Form(...), db: Session = Depends(get_db)):
    if confirm != "delete ranking observations":
        raise HTTPException(422, "Type delete ranking observations to confirm")
    db.query(DecisionEvent).delete(synchronize_session=False)
    profile = get_or_create_profile(db)
    profile.data = {k: v for k, v in (profile.data or {}).items() if k not in {"ranking_model", "outcome_model"}}
    profile.data = {**profile.data, "settings": {**(profile.data.get("settings") or {}), "record_decisions_enabled": False}}
    db.commit()
    return RedirectResponse("/today", 303)
