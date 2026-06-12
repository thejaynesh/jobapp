import uuid
import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.application import Application, ApplicationStatus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/apps", tags=["apps"])
templates = Jinja2Templates(directory="app/templates")


@router.get("", response_class=HTMLResponse)
def get_apps(request: Request, db: Session = Depends(get_db)):
    apps = db.query(Application).order_by(Application.created_at.desc()).all()
    return templates.TemplateResponse(
        "apps/index.html",
        {"request": request, "apps": apps},
    )


@router.post("/{app_id}/status", response_class=HTMLResponse)
def update_app_status(
    app_id: uuid.UUID,
    request: Request,
    status: str = Form(...),
    db: Session = Depends(get_db),
):
    app_obj = db.query(Application).filter(Application.id == app_id).first()
    if not app_obj:
        raise HTTPException(status_code=404, detail="Application not found")
    try:
        app_obj.status = ApplicationStatus(status)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid status: {status}")
    db.commit()
    return templates.TemplateResponse(
        "apps/partials/app_row.html",
        {"request": request, "app": app_obj},
    )
