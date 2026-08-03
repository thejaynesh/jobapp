import errno
import logging
import uuid

from celery.exceptions import SoftTimeLimitExceeded

from app.celery_app import celery_app
from app.database import SessionLocal
from app.models.application import Application

logger = logging.getLogger(__name__)


def _friendly_error(exc: BaseException) -> str:
    if isinstance(exc, OSError) and exc.errno == errno.EAGAIN:
        return (
            "The server was temporarily out of resources (memory/processes). "
            "It usually recovers in a minute — please retry."
        )
    return str(exc)


@celery_app.task(
    name="app.tasks.generate.generate_docs",
    bind=True,
    soft_time_limit=300,
    time_limit=360,
    max_retries=2,
)
def generate_docs(self, application_id: str, feedback: str | None = None) -> dict:
    db = SessionLocal()
    try:
        app = db.query(Application).filter(Application.id == uuid.UUID(application_id)).first()
        if not app:
            logger.warning("generate_docs: application %s not found", application_id)
            return {"status": "not_found"}

        app.generation_status = "generating"
        app.generation_error = None
        db.commit()

        from app.services.doc_generator import generate_documents
        generate_documents(db, app, feedback=feedback)

        app = db.query(Application).filter(Application.id == uuid.UUID(application_id)).first()
        if app:
            app.generation_status = "done"
            db.commit()

        return {"status": "ok", "application_id": application_id}

    except SoftTimeLimitExceeded:
        logger.error("generate_docs timed out for %s", application_id)
        _mark_failed(db, application_id, "Generation timed out after 5 minutes")
        return {"status": "timeout"}

    except Exception as exc:
        db.rollback()
        # Transient resource exhaustion (fork/thread EAGAIN): retry the whole
        # task after a pause instead of failing the generation outright.
        if (
            isinstance(exc, OSError)
            and exc.errno == errno.EAGAIN
            and self.request.retries < self.max_retries
        ):
            logger.warning(
                "generate_docs EAGAIN for %s — retrying task (attempt %d)",
                application_id, self.request.retries + 1,
            )
            raise self.retry(countdown=20, exc=exc)
        logger.error("generate_docs failed for %s: %s", application_id, exc)
        _mark_failed(db, application_id, _friendly_error(exc))
        return {"status": "error", "error": str(exc)}
    finally:
        db.close()


def _mark_failed(db, application_id: str, error: str) -> None:
    try:
        app = db.query(Application).filter(Application.id == uuid.UUID(application_id)).first()
        if app:
            app.generation_status = "failed"
            app.generation_error = error[:500]
            db.commit()
    except Exception as exc:
        logger.error("generate_docs: could not save failure state: %s", exc)
