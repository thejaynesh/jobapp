import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.job import Job, JobStatus
from app.tasks.generate import generate_docs

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/jobs", tags=["docs"])


class GenerateDocsRequest(BaseModel):
    feedback: Optional[str] = None


@router.post("/{job_id}/generate-docs", status_code=202)
def trigger_generate_docs(
    job_id: uuid.UUID,
    body: GenerateDocsRequest = GenerateDocsRequest(),
    db: Session = Depends(get_db),
):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status not in (JobStatus.matched, JobStatus.docs_generated):
        raise HTTPException(status_code=422, detail="Job must be in matched or docs_generated status")
    for app in job.applications:
        generate_docs.delay(str(app.id), feedback=body.feedback)
    return {"queued": len(job.applications)}
