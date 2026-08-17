import logging

from app.celery_app import celery_app
from app.config import settings
from app.database import SessionLocal

logger = logging.getLogger(__name__)

_EMPTY = {"checked": 0, "closed": 0, "still_open": 0, "unknown": 0}


@celery_app.task(
    name="app.tasks.liveness.check_postings",
    bind=False,
    # A sweep is bounded HTTP calls; well under the broker's redelivery window.
    soft_time_limit=900,
    time_limit=1020,
)
def check_postings(limit: int | None = None) -> dict:
    """
    Mark closed postings among the jobs worth applying to.

    Budgeted per run and self-pacing through `liveness_checked_at`, so the
    same job is not re-fetched every sweep and a large backlog drains over a
    few cycles rather than hammering anyone.
    """
    if not settings.LIVENESS_ENABLED:
        return {**_EMPTY, "skipped_reason": "disabled"}

    from app.services.liveness import sweep

    db = SessionLocal()
    try:
        return sweep(db, limit=limit)
    except Exception as exc:
        logger.error("check_postings failed: %s", exc)
        db.rollback()
        return dict(_EMPTY)
    finally:
        db.close()
