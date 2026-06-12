import logging

from app.celery_app import celery_app
from app.database import SessionLocal
from app.services.job_fetcher import fetch_and_save_jobs

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.fetch.fetch_jobs", bind=True, max_retries=0)
def fetch_jobs(self) -> dict:
    db = SessionLocal()
    try:
        result = fetch_and_save_jobs(db)
        logger.info(
            "fetch_jobs complete — fetched=%d inserted=%d merged=%d skipped=%d",
            result["fetched"], result["inserted"], result["merged"], result["skipped"],
        )
        return result
    except Exception as exc:
        logger.error("fetch_jobs task raised unexpectedly: %s", exc)
        return {"fetched": 0, "inserted": 0, "merged": 0, "skipped": 0}
    finally:
        db.close()
