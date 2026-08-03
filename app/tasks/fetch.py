import logging

from app.celery_app import celery_app
from app.database import SessionLocal
from app.services.fetch_lock import acquire, release
from app.services.job_fetcher import fetch_and_save_jobs

logger = logging.getLogger(__name__)

_EMPTY = {"fetched": 0, "inserted": 0, "merged": 0, "skipped": 0}


@celery_app.task(name="app.tasks.fetch.fetch_jobs", bind=True, max_retries=0)
def fetch_jobs(self, only: list[str] | None = None, match_after: bool = True) -> dict:
    """
    One fetch cycle.

    `only` restricts the run to the named sources — the scheduled cycle passes
    nothing and fetches everything, while a manual test run can ask for one
    adapter and finish in seconds.

    Held under a lock so a manual trigger can't overlap the scheduled cycle:
    two at once would double every outbound request and make the per-source
    numbers meaningless.
    """
    if not acquire():
        logger.warning("fetch_jobs: another fetch is already running; skipping")
        return {**_EMPTY, "skipped_reason": "already running"}

    db = SessionLocal()
    try:
        result = fetch_and_save_jobs(db, only=set(only) if only else None)
        logger.info(
            "fetch_jobs complete — fetched=%d inserted=%d merged=%d skipped=%d",
            result["fetched"], result["inserted"], result["merged"], result["skipped"],
        )
        if match_after:
            from app.tasks.match import match_jobs
            match_jobs.delay()
        return result
    except Exception as exc:
        logger.error("fetch_jobs task raised unexpectedly: %s", exc)
        return dict(_EMPTY)
    finally:
        db.close()
        release()
