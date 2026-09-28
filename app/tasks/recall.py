"""
The daily coverage check (`services.source_yield`): how many of SimplifyJobs'
postings our own readers also found, and what each source found alone. Reads
the database only. Runnable by hand:

    docker compose -f docker-compose.prod.yml exec worker python -m app.tasks.recall
"""

import logging

from app.celery_app import celery_app
from app.database import SessionLocal

logger = logging.getLogger(__name__)


def measure_and_store(db) -> dict:
    """Measure, keep the result on the profile, and return it."""
    from app.models.profile import Profile
    from app.services import source_yield

    profile = db.query(Profile).first()
    data = dict(profile.data or {}) if profile else {}
    state = source_yield.measure(db, data)
    if profile is not None:
        profile.data = {**data, source_yield.STATE_KEY: state}
        db.commit()
    return state


@celery_app.task(name="app.tasks.recall.measure_coverage", bind=False,
                 soft_time_limit=600, time_limit=660)
def measure_coverage() -> dict:
    db = SessionLocal()
    try:
        latest = measure_and_store(db)["latest"]
        logger.info("coverage: %s of %s SimplifyJobs postings found by another source "
                    "(%s%% of those matching the roles)",
                    latest["found"], latest["total"], latest["roles_share"])
        return {k: v for k, v in latest.items() if k != "per_source"}
    except Exception as exc:
        db.rollback()
        logger.warning("coverage: measurement failed: %s", exc)
        return {"error": str(exc)}
    finally:
        db.close()


if __name__ == "__main__":
    import json

    logging.basicConfig(level=logging.INFO)
    print(json.dumps(measure_coverage(), indent=2, default=str))
