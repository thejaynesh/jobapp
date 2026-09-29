"""Reminders for the next actions on applications that have come due."""

import logging

from app.celery_app import celery_app
from app.database import SessionLocal

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.tracker.remind_due_actions", bind=False,
                 soft_time_limit=60, time_limit=90)
def remind_due_actions() -> dict:
    """One warning in the log listing what is due, so the Log badge counts it."""
    from app.services import tracker

    db = SessionLocal()
    try:
        due = tracker.due(db)
        line = tracker.reminder_line(due)
        if line:
            logger.warning(line)
        return {"due": len(due)}
    finally:
        db.close()
