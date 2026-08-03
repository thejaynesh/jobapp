import copy
import logging
import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request
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


def _board_registry(db: Session) -> dict:
    """Per-ATS board counts; never let a registry hiccup break the page."""
    try:
        from app.services.company_boards import summary
        return summary(db)
    except Exception as exc:
        logger.warning("settings: board registry summary failed: %s", exc)
        return {}


def _retired_boards(db: Session) -> list:
    """Boards that stopped returning jobs and are no longer polled."""
    try:
        from app.services.company_boards import retired_boards
        # Materialise here: a lazy/failed result blowing up mid-render would
        # take the whole settings page down.
        return list(retired_boards(db))
    except Exception as exc:
        logger.warning("settings: retired board lookup failed: %s", exc)
        return []


def _page_context(request: Request, profile, db: Session, settings_data: dict,
                  saved: bool) -> dict:
    return {
        "request": request,
        "settings": settings_data,
        "saved": saved,
        "last_fetch": profile.data.get("last_fetch"),
        "board_registry": _board_registry(db),
        "retired_boards": _retired_boards(db),
        # Configured slugs the validator could not make work — the other kind
        # of "not working" board, and previously computed but never shown.
        "slug_report": profile.data.get("ats_slug_report") or {},
    }


@router.get("", response_class=HTMLResponse)
def get_settings(request: Request, db: Session = Depends(get_db)):
    profile = get_or_create_profile(db)
    db.commit()
    current = {**_DEFAULTS, **profile.data.get("settings", {})}
    return templates.TemplateResponse(
        "settings/index.html", _page_context(request, profile, db, current, False)
    )


@router.post("/boards/{board_id}/reactivate", response_class=HTMLResponse)
def reactivate_board(board_id: uuid.UUID, db: Session = Depends(get_db)):
    """Put a retired board back into rotation, e.g. after fixing its slug."""
    from app.services.company_boards import reactivate

    board = reactivate(db, board_id)
    if board is None:
        raise HTTPException(status_code=404, detail="Board not found")
    db.commit()
    # The row removes itself from the "not working" list.
    return HTMLResponse("")


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
    return templates.TemplateResponse(
        "settings/index.html",
        _page_context(request, profile, db, new_data["settings"], True),
    )
