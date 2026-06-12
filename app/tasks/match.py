import logging
from typing import Any

from app.celery_app import celery_app
from app.database import SessionLocal
from app.services.matcher import match_all_new_jobs

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.match.match_jobs", bind=False)
def match_jobs() -> dict[str, Any]:
    from app.models.job import Job, JobStatus
    db = SessionLocal()
    try:
        result = match_all_new_jobs(db)
        matched_jobs = db.query(Job).filter(Job.status == JobStatus.matched).all()
        for job in matched_jobs:
            for app in job.applications:
                from app.tasks.generate import generate_docs
                generate_docs.delay(str(app.id))
        logger.info(
            "match_jobs complete — processed=%d matched=%d filtered_out=%d errors=%d",
            result["processed"], result["matched"], result["filtered_out"], result["errors"],
        )
        return result
    except Exception as exc:
        logger.error("match_jobs task raised unexpectedly: %s", exc)
        return {"processed": 0, "matched": 0, "filtered_out": 0, "errors": 1}
    finally:
        db.close()
