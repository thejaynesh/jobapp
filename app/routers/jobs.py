import uuid
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.job import Job, JobStatus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs", tags=["jobs"])
templates = Jinja2Templates(directory="app/templates")

_FILTERABLE_STATUSES = [JobStatus.matched, JobStatus.filtered_out, JobStatus.docs_generated]


@router.get("", response_class=HTMLResponse)
def get_jobs(request: Request, status: str = "", q: str = "", db: Session = Depends(get_db)):
    query = db.query(Job).filter(Job.status.in_(_FILTERABLE_STATUSES))
    if status:
        try:
            query = query.filter(Job.status == JobStatus(status))
        except ValueError:
            pass
    if q:
        query = query.filter(
            (Job.title.ilike(f"%{q}%")) | (Job.company.ilike(f"%{q}%"))
        )
    jobs = query.order_by(Job.llm_score.desc().nullslast()).all()
    return templates.TemplateResponse(
        "jobs/index.html",
        {"request": request, "jobs": jobs, "status_filter": status, "q": q},
    )


@router.post("/{job_id}/override", response_class=HTMLResponse)
def override_job_status(job_id: uuid.UUID, request: Request, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status == JobStatus.matched:
        job.status = JobStatus.filtered_out
    elif job.status == JobStatus.filtered_out:
        job.status = JobStatus.matched
    db.commit()
    return templates.TemplateResponse(
        "jobs/partials/job_card.html",
        {"request": request, "job": job},
    )
