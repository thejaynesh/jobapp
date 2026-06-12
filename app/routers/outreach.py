import uuid
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.application import Application
from app.services.outreach import run_outreach

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/apps", tags=["outreach"])


@router.post("/{app_id}/outreach", status_code=202)
def trigger_outreach(app_id: uuid.UUID, db: Session = Depends(get_db)):
    app_obj = db.query(Application).filter(Application.id == app_id).first()
    if not app_obj:
        raise HTTPException(status_code=404, detail="Application not found")
    run_outreach(db, app_obj)
    return {"status": "ok"}
