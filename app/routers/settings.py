import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from app.templating import build as build_templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.profile_service import get_or_create_profile

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings", tags=["settings"])
templates = build_templates()


def _settings_context(profile) -> dict:
    """
    Current value and env default for every tunable, grouped for the page.

    Declared once in `services.tunables` and rendered from that declaration —
    the previous hand-written trio of fields was written to a key nothing read,
    so all three did nothing at all.
    """
    from app.services import tunables

    data = profile.data if profile else {}
    return {
        "tunable_groups": [
            (group, [
                {
                    "spec": t,
                    "value": tunables.value(data, t.key),
                    "default": tunables.default(t),
                    "overridden": tunables.is_overridden(data, t.key),
                }
                for t in tunables.TUNABLES if t.group == group
            ])
            for group in tunables.GROUPS
        ],
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


def _page_context(request: Request, profile, db: Session, saved: bool) -> dict:
    return {
        "request": request,
        "saved": saved,
        **_settings_context(profile),
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
    return templates.TemplateResponse(
        "settings/index.html", _page_context(request, profile, db, False)
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
async def save_settings(request: Request, db: Session = Depends(get_db)):
    """
    Save whatever tunables the form submitted.

    Read from the raw form rather than declared as parameters: the fields come
    from the `TUNABLES` declaration, and duplicating them here is exactly how
    the old version ended up saving three values nobody read.
    """
    from app.services import tunables

    form = dict(await request.form())
    profile = get_or_create_profile(db)
    profile.data = tunables.apply_to_profile(profile.data, tunables.parse_form(form))
    db.commit()
    return templates.TemplateResponse(
        "settings/index.html", _page_context(request, profile, db, True)
    )
