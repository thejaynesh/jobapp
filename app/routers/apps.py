import html
import os
import uuid
import logging
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse
from app.templating import build as build_templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.application import Application, ApplicationDocument, ApplicationStatus, DocType
from app.tasks.generate import generate_docs

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/apps", tags=["apps"])
templates = build_templates()


@router.get("", response_class=HTMLResponse)
def get_apps(
    request: Request,
    status: str = "",
    q: str = "",
    sort: str = "newest",
    db: Session = Depends(get_db),
):
    from sqlalchemy import func as sa_func, or_
    from app.models.job import Job

    query = db.query(Application).join(Application.job)
    total_count = query.count()

    if status:
        try:
            query = query.filter(Application.status == ApplicationStatus(status))
        except ValueError:
            pass
    if q:
        pattern = f"%{q}%"
        query = query.filter(
            or_(Job.title.ilike(pattern), Job.company.ilike(pattern))
        )

    filtered_count = query.count()

    _EFFECTIVE_SCORE = sa_func.coalesce(Job.llm_score_deep, Job.llm_score)
    sort_map = {
        "newest": Application.created_at.desc(),
        "oldest": Application.created_at.asc(),
        "company": Job.company.asc(),
        "score": _EFFECTIVE_SCORE.desc().nullslast(),
    }
    order = sort_map.get(sort, sort_map["newest"])
    apps = query.order_by(order).all()

    return templates.TemplateResponse(
        "apps/index.html",
        {
            "request": request,
            "apps": apps,
            "total_count": total_count,
            "filtered_count": filtered_count,
            "status_filter": status,
            "q": q,
            "sort": sort,
            "rates": _rates(db),
        },
    )


def _rates(db: Session) -> dict | None:
    from app.services import tracker

    try:
        return tracker.response_rates(db)
    except Exception as exc:
        logger.warning("apps: response rates unavailable: %s", exc)
        return None


def _profile_data(db: Session) -> dict:
    from app.models.profile import Profile

    profile = db.query(Profile).first()
    return profile.data if profile is not None and isinstance(profile.data, dict) else {}


@router.get("/board", response_class=HTMLResponse)
def get_board(request: Request, db: Session = Depends(get_db)):
    """Applications in a column per status, each with what is next and when."""
    from app.services import tracker

    return templates.TemplateResponse(
        "apps/board.html",
        {"request": request, "columns": tracker.board(db), "today": datetime.now(timezone.utc).date()},
    )


@router.get("/docs/{doc_id}/download")
def download_doc(doc_id: uuid.UUID, db: Session = Depends(get_db)):
    doc = db.query(ApplicationDocument).filter(ApplicationDocument.id == doc_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if not os.path.exists(doc.path):
        raise HTTPException(status_code=404, detail="File not found on disk")
    filename = os.path.basename(doc.path)
    return FileResponse(doc.path, media_type="application/pdf", filename=filename)


@router.get("/{app_id}", response_class=HTMLResponse)
def get_app_detail(app_id: uuid.UUID, request: Request, db: Session = Depends(get_db)):
    app_obj = db.query(Application).filter(Application.id == app_id).first()
    if not app_obj:
        raise HTTPException(status_code=404, detail="Application not found")
    resumes = sorted(
        [d for d in app_obj.documents if d.doc_type == DocType.resume],
        key=lambda d: d.version,
        reverse=True,
    )
    cover_letters = sorted(
        [d for d in app_obj.documents if d.doc_type == DocType.cover_letter],
        key=lambda d: d.version,
        reverse=True,
    )
    from app.routers.outreach import panel_context
    from app.services import document_edit, letter_recipient

    return templates.TemplateResponse(
        "apps/detail.html",
        {
            "request": request,
            "resumes": resumes,
            "cover_letters": cover_letters,
            # What the current resume says, with the job's changes marked, for
            # the keyword check and the review-and-edit panel.
            "resume_review": document_edit.review(resumes[0].content) if resumes else None,
            "letter_body": (((cover_letters[0].content or {}).get("context") or {})
                            .get("cover_letter_body") if cover_letters else None),
            "letter_checks": ((cover_letters[0].content or {}).get("checks") or []
                              if cover_letters else []),
            # Who the current letter is addressed to, and who else it could be.
            "letter_recipient": ((((cover_letters[0].content or {}).get("context") or {})
                                  .get("recipient")) if cover_letters else None),
            "recipient_choices": [
                letter_recipient.recipient(c)
                for c in letter_recipient.candidates(app_obj.contacts)
            ],
            "stories_for_job": _stories_for(db, app_obj.job),
            "next_action_overdue": (
                isinstance(app_obj.next_action_due, date)
                and app_obj.next_action_due < datetime.now(timezone.utc).date()),
            # The page embeds the outreach panel partial, so it needs the same
            # context that /outreach/apps/{id}/panel builds.
            **panel_context(db, app_obj),
            # Pre-0012 discoveries, which only ever lived on the application.
            # Entries with neither a name nor an address are empty shells the
            # old code wrote when Hunter and LinkedIn both came back with nothing.
            "legacy_contacts": [
                c for c in (app_obj.outreach_contacts or [])
                if isinstance(c, dict) and (c.get("name") or c.get("email"))
            ],
            **_interview_context(db, app_obj),
        },
    )


def _interview_context(db: Session, app_obj) -> dict:
    """
    What the corpus knows about interviewing at this company.

    Wrapped, like every other panel: an empty corpus is the normal state for a
    long time, and a lookup that fails should cost a note rather than the
    application page somebody was actually trying to read.
    """
    from app.services.interview_corpus import coverage, reports_for

    company = (app_obj.job.company if app_obj.job else "") or ""
    if not company:
        return {"interview_reports": [], "interview_coverage": None, "interview_company": ""}
    try:
        return {
            "interview_company": company,
            "interview_reports": reports_for(db, company, limit=8),
            "interview_coverage": coverage(db, company),
        }
    except Exception as exc:
        logger.warning("apps: interview corpus unavailable: %s", exc)
        return {"interview_reports": [], "interview_coverage": None, "interview_company": company}


@router.post("/{app_id}/interview-research", response_class=HTMLResponse)
def research_interviews(app_id: uuid.UUID, request: Request, db: Session = Depends(get_db)):
    """
    Gather interview writeups for this company, now.

    The design rule this follows: automation decides when something usually
    happens, the user decides when it happens now. The mailbox poller is meant
    to fire this on an interview invite; this is the same work on demand,
    because "I have an interview on Thursday" arrives before any automation
    notices.

    Runs inline rather than through Celery. It is a handful of HTTP calls, the
    user is waiting on the answer, and a queued job that fails silently is a
    worse experience than a slow button.

    The database connection is deliberately let go before those calls. Fetching
    three sources can take a minute — GeeksforGeeks alone reads an index and
    then up to ten articles — and holding a pooled connection idle for that long
    while waiting on someone else's web server is how a handful of clicks
    exhausts the pool and takes down every page in the app.
    """
    app_obj = db.query(Application).filter(Application.id == app_id).first()
    if not app_obj:
        raise HTTPException(status_code=404, detail="Application not found")

    company = (app_obj.job.company if app_obj.job else "") or ""
    # Read into a local first: after the commit below these objects are expired,
    # and touching one would silently take a connection straight back out.
    outcome = None
    if company:
        # Ends the read transaction and returns the connection to the pool. The
        # session reacquires one by itself when ingestion needs it.
        db.commit()
        try:
            from app.services.interview_corpus import ingest
            from app.services.interview_sources import fetch_all

            fetched = fetch_all(company)
            counts = ingest(db, fetched["reports"])
            outcome = {**counts, "sources": fetched["sources"]}

            # A source that refused this server for being a server has a
            # different remedy from one that erred: ask again from the browser.
            # Nothing waits for it — the answer lands in the corpus whenever the
            # laptop next polls, and the panel says so rather than appearing to
            # have found nothing.
            if any(s.get("blocked") for s in fetched["sources"].values()):
                from app.services.agent_work import enqueue_reddit_search

                outcome["queued_to_browser"] = enqueue_reddit_search(db, company)
        except Exception as exc:
            logger.error("apps: interview research failed for %s: %s", company, exc)
            outcome = {"error": str(exc)}

    return templates.TemplateResponse(
        "apps/partials/interview_panel.html",
        {
            "request": request,
            "app": app_obj,
            "research_outcome": outcome,
            **_interview_context(db, app_obj),
        },
    )


@router.post("/{app_id}/status", response_class=HTMLResponse)
def update_app_status(
    app_id: uuid.UUID,
    request: Request,
    status: str = Form(...),
    fragment: str = "",
    db: Session = Depends(get_db),
):
    app_obj = db.query(Application).filter(Application.id == app_id).first()
    if not app_obj:
        raise HTTPException(status_code=404, detail="Application not found")
    from app.services import tracker

    try:
        new_status = ApplicationStatus(status)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid status: {status}")
    # The time, what was sent, and the next action move with the status.
    tracker.set_status(db, app_obj, new_status, profile_data=_profile_data(db))
    db.commit()
    if fragment == "board":
        return HTMLResponse("", headers={"HX-Refresh": "true"})
    # The detail page swaps only a small confirmation badge; the apps list
    # swaps the whole card.
    if fragment == "badge":
        return templates.TemplateResponse(
            "apps/partials/status_badge.html",
            {"request": request, "app": app_obj},
        )
    return templates.TemplateResponse(
        "apps/partials/app_card.html",
        {"request": request, "app": app_obj},
    )


@router.post("/{app_id}/next-action", response_class=HTMLResponse)
def save_next_action(app_id: uuid.UUID, next_action: str = Form(""), due: str = Form(""),
                     db: Session = Depends(get_db)):
    """Your own next step and its date, in place of the default."""
    from datetime import date as date_type

    from app.services import tracker

    app_obj = db.query(Application).filter(Application.id == app_id).first()
    if not app_obj:
        raise HTTPException(status_code=404, detail="Application not found")
    try:
        due_date = date_type.fromisoformat(due) if due else None
    except ValueError:
        raise HTTPException(status_code=422, detail="Due date must be YYYY-MM-DD")
    tracker.set_next_action(app_obj, next_action, due_date)
    db.commit()
    return HTMLResponse('<span class="text-xs text-green-600">Saved</span>')


@router.post("/{app_id}/sent-letter", response_class=HTMLResponse)
def save_sent_letter(app_id: uuid.UUID, sent: str = Form(""), db: Session = Depends(get_db)):
    """Whether a cover letter actually went with this application."""
    app_obj = db.query(Application).filter(Application.id == app_id).first()
    if not app_obj:
        raise HTTPException(status_code=404, detail="Application not found")
    app_obj.sent_cover_letter = sent == "1"
    db.commit()
    return HTMLResponse('<span class="text-xs text-green-600">Saved</span>')


@router.post("/{app_id}/notes", response_class=HTMLResponse)
def save_notes(
    app_id: uuid.UUID,
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    app_obj = db.query(Application).filter(Application.id == app_id).first()
    if not app_obj:
        raise HTTPException(status_code=404, detail="Application not found")
    app_obj.notes = notes
    db.commit()
    return HTMLResponse('<span class="text-xs text-green-600">Saved</span>')


def _stories_for(db: Session, job) -> list[dict]:
    """The story bank's best fits for this posting, for interview preparation."""
    from app.models.profile import Profile
    from app.services import stories
    from app.services.profile_service import for_documents

    profile = db.query(Profile).first()
    if profile is None or job is None:
        return []
    text = " ".join(str(x) for x in (
        job.title, job.description, " ".join(job.required_skills or []),
        " ".join(job.nice_to_have_skills or [])) if x)
    return stories.relevant(for_documents(profile.data or {}), text)


def _document(db: Session, app_id: uuid.UUID, doc_id: uuid.UUID):
    app_obj = db.query(Application).filter(Application.id == app_id).first()
    doc = db.query(ApplicationDocument).filter(
        ApplicationDocument.id == doc_id,
        ApplicationDocument.application_id == app_id,
    ).first()
    if app_obj is None or doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return app_obj, doc


def _edited(app_obj, save) -> HTMLResponse:
    """Run an edit; reload the page on success, say what went wrong otherwise."""
    from app.services.doc_generator import DocGenerationError
    from app.services.document_edit import NotEditable

    try:
        save()
    except NotEditable as exc:
        return HTMLResponse(f'<span class="text-amber-700">{html.escape(str(exc))}</span>')
    except DocGenerationError as exc:
        return HTMLResponse('<span class="text-red-600">The PDF would not compile: '
                            f"{html.escape(str(exc)[:300])}</span>")
    return HTMLResponse("", headers={"HX-Redirect": f"/apps/{app_obj.id}"})


@router.post("/{app_id}/docs/{doc_id}/edit-resume", response_class=HTMLResponse)
async def edit_resume(app_id: uuid.UUID, doc_id: uuid.UUID, request: Request,
                      db: Session = Depends(get_db)):
    """Save the review panel's summary, bullets and skills as the next resume version."""
    from app.services import document_edit

    app_obj, doc = _document(db, app_id, doc_id)
    form = dict(await request.form())
    return await run_in_threadpool(
        _edited, app_obj, lambda: document_edit.save_resume(db, app_obj, doc, form))


@router.post("/{app_id}/docs/{doc_id}/edit-letter", response_class=HTMLResponse)
def edit_letter(app_id: uuid.UUID, doc_id: uuid.UUID, body: str = Form(""),
                recipient: str = Form("keep"), db: Session = Depends(get_db)):
    """
    Save an edited letter body, and who it is addressed to, as the next cover
    letter version. `recipient` is one of the application's contacts by id,
    "none" for "Dear Hiring Manager", or "keep".
    """
    from app.services import document_edit, letter_recipient

    app_obj, doc = _document(db, app_id, doc_id)
    extra = {}
    if recipient == "none":
        extra["recipient"] = None
    elif recipient != "keep":
        chosen = next((c for c in letter_recipient.candidates(app_obj.contacts)
                       if str(c.id) == recipient), None)
        if chosen is None:
            raise HTTPException(status_code=404, detail="No such contact on this application")
        extra["recipient"] = letter_recipient.recipient(chosen)
    return _edited(app_obj, lambda: document_edit.save_letter(db, app_obj, doc, body, **extra))


@router.post("/{app_id}/regenerate", response_class=HTMLResponse)
def regenerate_docs(
    app_id: uuid.UUID,
    feedback: str = Form(""),
    db: Session = Depends(get_db),
):
    app_obj = db.query(Application).filter(Application.id == app_id).first()
    if not app_obj:
        raise HTTPException(status_code=404, detail="Application not found")
    app_obj.generation_status = "generating"
    app_obj.generation_error = None
    # Stamped at queue time, not only at task start: with no clock on the row
    # the sweeper reads NULL as "stale" and queues a duplicate while this one
    # is still waiting for a worker.
    app_obj.generation_started_at = datetime.now(timezone.utc)
    db.commit()
    generate_docs.delay(str(app_obj.id), feedback=feedback or None)
    return HTMLResponse('<span class="text-blue-600">Queued &mdash; generating&hellip;</span>')
