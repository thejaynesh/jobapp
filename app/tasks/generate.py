import logging
import uuid

from app.celery_app import celery_app
from app.database import SessionLocal
from app.models.application import Application
from app.services.doc_generator import generate_documents

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.generate.generate_docs", bind=False)
def generate_docs(application_id: str, feedback: str | None = None) -> dict:
    db = SessionLocal()
    try:
        app = db.query(Application).filter(Application.id == uuid.UUID(application_id)).first()
        if not app:
            logger.warning("generate_docs: application %s not found", application_id)
            return {"status": "not_found"}
        generate_documents(db, app, feedback=feedback)
        return {"status": "ok", "application_id": application_id}
    except Exception as exc:
        logger.error("generate_docs failed for %s: %s", application_id, exc)
        return {"status": "error", "error": str(exc)}
    finally:
        db.close()
