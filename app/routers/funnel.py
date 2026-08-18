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

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.services import funnel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/funnel", tags=["funnel"])
templates = Jinja2Templates(directory="app/templates")

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
