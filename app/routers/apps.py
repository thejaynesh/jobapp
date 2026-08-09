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
