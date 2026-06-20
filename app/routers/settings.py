import copy
import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.profile_service import get_or_create_profile

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings", tags=["settings"])
templates = Jinja2Templates(directory="app/templates")

_DEFAULTS = {
    "min_match_score": 70,
    "fetch_interval_hours": 5,
    "min_keyword_skills": 2,
}


@router.get("", response_class=HTMLResponse)
def get_settings(request: Request, db: Session = Depends(get_db)):
    profile = get_or_create_profile(db)
    db.commit()
    current = {**_DEFAULTS, **profile.data.get("settings", {})}
    last_fetch = profile.data.get("last_fetch")
    return templates.TemplateResponse(
        "settings/index.html",
        {"request": request, "settings": current, "saved": False, "last_fetch": last_fetch},
    )


@router.post("", response_class=HTMLResponse)
def save_settings(
    request: Request,
    min_match_score: int = Form(70),
    fetch_interval_hours: int = Form(5),
    min_keyword_skills: int = Form(2),
    db: Session = Depends(get_db),
):
    profile = get_or_create_profile(db)
    new_data = copy.deepcopy(profile.data)
    new_data["settings"] = {
        "min_match_score": min_match_score,
        "fetch_interval_hours": fetch_interval_hours,
        "min_keyword_skills": min_keyword_skills,
    }
    profile.data = new_data
    db.commit()
    last_fetch = profile.data.get("last_fetch")
    return templates.TemplateResponse(
        "settings/index.html",
        {"request": request, "settings": new_data["settings"], "saved": True, "last_fetch": last_fetch},
    )
