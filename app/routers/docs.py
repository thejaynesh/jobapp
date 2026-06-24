import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.application import Application
from app.models.job import Job, JobStatus
from app.tasks.generate import generate_docs

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/jobs", tags=["docs"])
templates = Jinja2Templates(directory="app/templates")


class GenerateDocsRequest(BaseModel):
    feedback: Optional[str] = None


@router.post("/{job_id}/generate-docs", response_class=HTMLResponse)
def trigger_generate_docs(
    job_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status not in (JobStatus.matched, JobStatus.docs_generated):
        raise HTTPException(status_code=422, detail="Job must be matched or docs_generated")

    app_obj = job.applications[0] if job.applications else None
    if not app_obj:
        raise HTTPException(status_code=422, detail="No application found for this job")

    app_obj.generation_status = "generating"
    app_obj.generation_error = None
    db.commit()

    generate_docs.delay(str(app_obj.id))

    return templates.TemplateResponse(
        "jobs/partials/doc_gen_btn.html",
        {"request": request, "job": job, "app": app_obj},
    )


@router.get("/{job_id}/doc-status-html", response_class=HTMLResponse)
def doc_status_html(
    job_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    app_obj = job.applications[0] if job.applications else None

    return templates.TemplateResponse(
        "jobs/partials/doc_gen_btn.html",
        {"request": request, "job": job, "app": app_obj},
    )


@router.post("/{job_id}/cancel-generate", response_class=HTMLResponse)
def cancel_generate(
    job_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
):
    """Reset a stuck 'generating' application back to idle so the user can retry."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    app_obj = job.applications[0] if job.applications else None
    if app_obj:
        app_obj.generation_status = "idle"
        app_obj.generation_error = None
        db.commit()

    return templates.TemplateResponse(
        "jobs/partials/doc_gen_btn.html",
        {"request": request, "job": job, "app": app_obj},
    )
