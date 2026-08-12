import os
import uuid
import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.application import Application, ApplicationDocument, ApplicationStatus, DocType
from app.tasks.generate import generate_docs

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/apps", tags=["apps"])
templates = Jinja2Templates(directory="app/templates")


@router.get("", response_class=HTMLResponse)
def get_apps(request: Request, db: Session = Depends(get_db)):
    apps = db.query(Application).order_by(Application.created_at.desc()).all()
    return templates.TemplateResponse(
        "apps/index.html",
        {"request": request, "apps": apps},
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

    return templates.TemplateResponse(
        "apps/detail.html",
        {
            "request": request,
            "resumes": resumes,
            "cover_letters": cover_letters,
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
    """
    app_obj = db.query(Application).filter(Application.id == app_id).first()
    if not app_obj:
        raise HTTPException(status_code=404, detail="Application not found")

    company = (app_obj.job.company if app_obj.job else "") or ""
    outcome = None
    if company:
        try:
            from app.services.interview_corpus import ingest
            from app.services.interview_sources import fetch_all

            fetched = fetch_all(company)
            counts = ingest(db, fetched["reports"])
            outcome = {**counts, "sources": fetched["sources"]}
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
    try:
        app_obj.status = ApplicationStatus(status)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid status: {status}")
    db.commit()
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
    db.commit()
    generate_docs.delay(str(app_obj.id), feedback=feedback or None)
    return HTMLResponse('<span class="text-blue-600">Queued &mdash; generating&hellip;</span>')
