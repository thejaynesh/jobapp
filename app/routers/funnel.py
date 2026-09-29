"""
One page for the question the whole pipeline is for.

Every number here could already be got at with a query somebody was willing to
write. What could not be got at is the shape they make together — and a hundred
and fifty thousand jobs fetched against forty applications sent is either a
working filter or a broken one depending entirely on what happened in between.

Each section is wrapped separately. A dashboard is the page you open when
something is already wrong, so one query failing must cost that panel rather
than the view.
"""

import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from app.templating import build as build_templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.services import funnel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/funnel", tags=["funnel"])
templates = build_templates()

DEFAULT_DAYS = 30
# Fetch cycles to roll source ROI over. The same window the /runs page uses, so
# the two pages cannot disagree about what a source has been contributing.
ROLLUP_RUNS = 20


def _safe(name: str, call, fallback):
    try:
        return call()
    except Exception as exc:
        logger.warning("funnel: %s unavailable: %s", name, exc)
        return fallback


@router.get("", response_class=HTMLResponse)
def get_funnel(request: Request, days: int = DEFAULT_DAYS,
               db: Session = Depends(get_db)):
    days = max(1, min(days, 365))
    return templates.TemplateResponse(
        "funnel/index.html",
        {
            "request": request,
            "days": days,
            "overview": _safe("overview", lambda: funnel.overview(db), None),
            "cohorts": _safe("cohorts", lambda: funnel.cohorts(db, days), []),
            "sources": _safe("source roi",
                             lambda: funnel.source_roi(db, ROLLUP_RUNS), []),
            "scores": _safe("score distribution",
                            lambda: funnel.score_distribution(db), []),
            "second_opinion": _safe("second opinion",
                                    lambda: funnel.second_opinion(db), None),
            "enrichment": _safe("enrichment effect",
                                lambda: funnel.enrichment_effect(db), None),
            "rollup_runs": ROLLUP_RUNS,
        },
    )


def _profile(db: Session):
    from app.services.profile_service import get_or_create_profile

    return get_or_create_profile(db)


@router.get("/matching", response_class=HTMLResponse)
def get_matching(request: Request, db: Session = Depends(get_db)):
    """How the matcher's scores compare with what you then did, and what to change."""
    from app.services import for_you, match_report

    profile = _profile(db)
    return templates.TemplateResponse(
        "funnel/matching.html",
        {
            "request": request,
            "report": match_report.build(db, profile.data),
            "reason_labels": {k: v[0] for k, v in match_report.DISMISS_REASONS.items()},
            "ranking": (profile.data or {}).get(for_you.STORE_KEY),
            "ranking_min": (for_you.MIN_YES, for_you.MIN_NO),
        },
    )


@router.post("/matching/threshold", response_class=HTMLResponse)
def use_threshold(request: Request, value: int = Form(...), db: Session = Depends(get_db)):
    """Set the minimum match score the report suggested, as the settings page would."""
    from fastapi.responses import RedirectResponse

    from app.services import tunables

    profile = _profile(db)
    parsed = {"min_match_score": tunables.coerce(tunables.BY_KEY["min_match_score"], value)}
    if parsed["min_match_score"] is None:
        raise HTTPException(status_code=422, detail="Not a score")
    profile.data = tunables.apply_to_profile(profile.data or {}, parsed)
    db.commit()
    return RedirectResponse(url="/funnel/matching?saved=1", status_code=303)


@router.post("/matching/retrain", response_class=HTMLResponse)
def retrain(request: Request, db: Session = Depends(get_db)):
    """Train the "For you" ranking now instead of waiting for its schedule."""
    from fastapi.responses import RedirectResponse

    from app.services import for_you

    profile = _profile(db)
    for_you.save(db, for_you.fit(db, profile.data or {}))
    return RedirectResponse(url="/funnel/matching#ranking", status_code=303)
