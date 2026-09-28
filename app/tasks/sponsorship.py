"""
Employers' H-1B filing history, kept current from DOL's disclosure files
(`services.sponsorship_history`). Daily, and cheap when nothing is new: one
page is read to see whether DOL has published a quarter. Runnable by hand:

    docker compose -f docker-compose.prod.yml exec worker python -m app.tasks.sponsorship
"""

import logging

from app.celery_app import celery_app
from app.database import SessionLocal

logger = logging.getLogger(__name__)


@celery_app.task(
    name="app.tasks.sponsorship.refresh_h1b_history",
    bind=False,
    # A new quarter is a 100-250 MB workbook, read in two to four minutes;
    # the first run reads two or three of them.
    soft_time_limit=1800,
    time_limit=1860,
)
def refresh_h1b_history() -> dict:
    from app.models.profile import Profile
    from app.services import sponsorship_history

    db = SessionLocal()
    try:
        profile = db.query(Profile).first()
        data = dict(profile.data or {}) if profile else {}
        try:
            report = sponsorship_history.refresh(db, data)
        except Exception as exc:
            db.rollback()
            logger.warning("sponsorship: refresh failed: %s", exc)
            return {"ok": False, "error": str(exc)}
        state = report.pop("state", None)
        if profile is not None and state is not None:
            profile.data = {**data, sponsorship_history.STATE_KEY: state}
        db.commit()
        return {"ok": True, **report}
    finally:
        db.close()


if __name__ == "__main__":
    import json

    logging.basicConfig(level=logging.INFO)
    print(json.dumps(refresh_h1b_history(), indent=2, default=str))
