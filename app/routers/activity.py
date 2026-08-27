import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from app.templating import build as build_templates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/activity", tags=["activity"])
templates = build_templates()


@router.get("", response_class=HTMLResponse)
def activity_log(request: Request, severity: str = "", category: str = "",
                 limit: int = 100):
    from app.services import activity_log

    limit = max(1, min(limit, 200))
    events = activity_log.recent(
        limit=limit,
        severity=severity or None,
        category=category or None,
    )
    stats = activity_log.counts()
    return templates.TemplateResponse(
        "activity/index.html",
        {
            "request": request,
            "events": events,
            "stats": stats,
            "categories": activity_log.categories(),
            "severity_filter": severity,
            "category_filter": category,
            "limit": limit,
        },
    )


@router.get("/feed", response_class=HTMLResponse)
def activity_feed(request: Request, severity: str = "", category: str = "",
                  since: float = 0):
    """Partial for HTMX polling — returns just the event rows."""
    from app.services import activity_log

    events = activity_log.recent(
        limit=50,
        severity=severity or None,
        category=category or None,
        since=since if since > 0 else None,
    )
    return templates.TemplateResponse(
        "activity/partials/feed.html",
        {"request": request, "events": events},
    )


@router.get("/badge", response_class=HTMLResponse)
def activity_badge(request: Request):
    """Just the count badge, for the nav to poll."""
    from app.services import activity_log

    stats = activity_log.counts()
    return templates.TemplateResponse(
        "activity/partials/badge.html",
        {"request": request, "stats": stats},
    )


@router.post("/clear", response_class=HTMLResponse)
def clear_log(request: Request):
    from app.services import activity_log

    activity_log.clear()
    return templates.TemplateResponse(
        "activity/partials/feed.html",
        {"request": request, "events": []},
    )
