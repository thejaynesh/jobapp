import logging
import uuid
from datetime import datetime, timezone

from celery.exceptions import SoftTimeLimitExceeded

from app.celery_app import celery_app
from app.database import SessionLocal
from app.models.application import Application

logger = logging.getLogger(__name__)


@celery_app.task(
    name="app.tasks.outreach.discover_contacts_task",
    bind=True,
    soft_time_limit=300,
    time_limit=360,
)
def discover_contacts_task(self, application_id: str, draft: bool = True) -> dict:
    """
    Find contacts for one application, off the request thread.

    Discovery can spend a minute on Hunter plus a browser launch for LinkedIn,
    which is far too long to hold a web request open. Progress is reported
    through `Application.outreach_status`, which the panel polls.
    """
    db = SessionLocal()
    try:
        app = db.query(Application).filter(Application.id == uuid.UUID(application_id)).first()
        if not app:
            logger.warning("discover_contacts_task: application %s not found", application_id)
            return {"status": "not_found"}

        app.outreach_status = "discovering"
        app.outreach_error = None
        # Restamped here as well as at queue time: a task that sat in the broker
        # for an hour should get the full window from when it actually started.
        app.outreach_checked_at = datetime.now(timezone.utc)
        db.commit()

        from app.services.outreach import run_outreach
        contacts = run_outreach(db, app, draft=draft)

        app.outreach_status = "done"
        db.commit()
        return {"status": "ok", "application_id": application_id, "contacts": len(contacts)}

    except SoftTimeLimitExceeded:
        logger.error("discover_contacts_task timed out for %s", application_id)
        _mark_failed(db, application_id, "Contact discovery timed out after 5 minutes")
        return {"status": "timeout"}
    except Exception as exc:
        db.rollback()
        logger.error("discover_contacts_task failed for %s: %s", application_id, exc)
        _mark_failed(db, application_id, str(exc))
        return {"status": "error", "error": str(exc)}
    finally:
        db.close()


@celery_app.task(name="app.tasks.outreach.draft_message_task", soft_time_limit=180)
def draft_message_task(
    contact_id: str, channel: str | None = None, kind: str = "initial",
    tone: str = "warm", feedback: str | None = None,
) -> dict:
    """Write one message for one contact (the UI's regenerate path when queued)."""
    from app.models.outreach import Contact
    from app.services.outreach import draft_message

    db = SessionLocal()
    try:
        contact = db.query(Contact).filter(Contact.id == uuid.UUID(contact_id)).first()
        if not contact:
            return {"status": "not_found"}
        message = draft_message(db, contact, channel=channel, kind=kind, tone=tone, feedback=feedback)
        return {"status": "ok", "message_id": str(message.id)}
    except Exception as exc:
        db.rollback()
        logger.error("draft_message_task failed for contact %s: %s", contact_id, exc)
        return {"status": "error", "error": str(exc)}
    finally:
        db.close()


@celery_app.task(name="app.tasks.outreach.process_followups", soft_time_limit=600)
def process_followups() -> dict:
    """
    Draft every follow-up that has come due. Runs on the beat schedule.

    Drafts only. Nothing is sent without someone clicking send, so a scheduler
    that runs while nobody is looking can never mail anyone.
    """
    from app.config import settings

    if not (settings.OUTREACH_ENABLED and settings.OUTREACH_AUTO_DRAFT_FOLLOWUPS):
        return {"status": "disabled"}

    db = SessionLocal()
    try:
        from app.services.outreach import draft_due_follow_ups
        drafted = draft_due_follow_ups(db)
        return {"status": "ok", "drafted": len(drafted)}
    except Exception as exc:
        db.rollback()
        logger.error("process_followups failed: %s", exc)
        return {"status": "error", "error": str(exc)}
    finally:
        db.close()


@celery_app.task(name="app.tasks.outreach.send_message_task", soft_time_limit=120)
def send_message_task(message_id: str, allow_guessed: bool = False) -> dict:
    """Deliver one already-drafted email. Only ever queued by an explicit send."""
    from app.models.outreach import OutreachMessage
    from app.services.outreach_sender import SendError, send_message

    db = SessionLocal()
    try:
        message = (
            db.query(OutreachMessage)
            .filter(OutreachMessage.id == uuid.UUID(message_id))
            .first()
        )
        if not message:
            return {"status": "not_found"}
        send_message(db, message, allow_guessed=allow_guessed)
        return {"status": "ok", "message_id": message_id}
    except SendError as exc:
        return {"status": "error", "error": str(exc)}
    except Exception as exc:
        db.rollback()
        logger.error("send_message_task failed for %s: %s", message_id, exc)
        return {"status": "error", "error": str(exc)}
    finally:
        db.close()


def _mark_failed(db, application_id: str, error: str) -> None:
    try:
        app = db.query(Application).filter(Application.id == uuid.UUID(application_id)).first()
        if app:
            app.outreach_status = "failed"
            app.outreach_error = error[:500]
            db.commit()
    except Exception as exc:
        logger.error("discover_contacts_task: could not save failure state: %s", exc)
