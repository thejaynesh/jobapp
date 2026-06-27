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
    return templates.TemplateResponse(
        "apps/detail.html",
        {
            "request": request,
            "app": app_obj,
            "resumes": resumes,
            "cover_letters": cover_letters,
        },
    )


@router.post("/{app_id}/status", response_class=HTMLResponse)
def update_app_status(
    app_id: uuid.UUID,
    request: Request,
    status: str = Form(...),
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
