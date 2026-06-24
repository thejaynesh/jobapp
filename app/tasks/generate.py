import logging
import uuid

from app.celery_app import celery_app
from app.database import SessionLocal
from app.models.application import Application

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.generate.generate_docs", bind=False)
def generate_docs(application_id: str, feedback: str | None = None) -> dict:
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

        # Re-fetch after generate_documents commits
        app = db.query(Application).filter(Application.id == uuid.UUID(application_id)).first()
        if app:
            app.generation_status = "done"
            db.commit()

        return {"status": "ok", "application_id": application_id}

    except Exception as exc:
        logger.error("generate_docs failed for %s: %s", application_id, exc)
        try:
            app = db.query(Application).filter(Application.id == uuid.UUID(application_id)).first()
            if app:
                app.generation_status = "failed"
                app.generation_error = str(exc)[:500]
                db.commit()
        except Exception as inner:
            logger.error("generate_docs: could not save failure state: %s", inner)
        return {"status": "error", "error": str(exc)}
    finally:
        db.close()
