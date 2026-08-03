import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/runs", tags=["runs"])
templates = Jinja2Templates(directory="app/templates")

# Enough to see a trend without turning the page into a wall.
DEFAULT_RUNS_SHOWN = 15
ROLLUP_WINDOW = 20

# Every source the fetcher knows about, for the manual-trigger picker.
TRIGGERABLE_SOURCES = [
    "adzuna", "jsearch", "linkedin", "greenhouse", "lever", "ashby",
    "smartrecruiters", "workable", "recruitee", "workday", "jooble",
    "careerjet", "findwork", "indeed", "remotive", "arbeitnow", "remoteok",
    "weworkremotely", "themuse", "himalayas", "jobicy", "hnhiring",
    "wellfound", "dice", "handshake",
]


@router.get("", response_class=HTMLResponse)
def get_runs(request: Request, limit: int = DEFAULT_RUNS_SHOWN,
             db: Session = Depends(get_db)):
    """Fetch-cycle history: what each run did, and what each source contributed."""
    from app.services.fetch_history import recent_runs, source_totals

    limit = max(1, min(limit, 100))
    try:
        runs = recent_runs(db, limit)
        totals = source_totals(db, ROLLUP_WINDOW)
    except Exception as exc:
        logger.warning("runs: history unavailable: %s", exc)
        runs, totals = [], []

    boards = []
    try:
        from app.models.company_board import CompanyBoard
        boards = (
            db.query(CompanyBoard)
            .filter(CompanyBoard.total_job_count > 0)
            .order_by(CompanyBoard.total_job_count.desc())
            .limit(25)
            .all()
        )
    except Exception as exc:
        logger.warning("runs: board leaderboard unavailable: %s", exc)

    from app.services.fetch_lock import state

    return templates.TemplateResponse(
        "runs/index.html",
        {
            "request": request,
            "runs": runs,
            "source_totals": totals,
            "rollup_window": ROLLUP_WINDOW,
            "top_boards": boards,
            "fetch_state": state(),
            "sources": TRIGGERABLE_SOURCES,
            "triggered": None,
        },
    )


@router.get("/status", response_class=HTMLResponse)
def fetch_status(request: Request):
    """Live fetch state; the page polls this while a run is in flight."""
    from app.services.fetch_lock import state
    return templates.TemplateResponse(
        "runs/partials/status.html",
        {"request": request, "fetch_state": state(), "triggered": None},
    )


@router.post("/trigger", response_class=HTMLResponse)
def trigger_fetch(request: Request, sources: list[str] = Form(default=[])):
    """
    Queue a fetch cycle now.

    Restricting it to a few sources is the point: a full cycle takes minutes
    (hundreds of company boards, LinkedIn pagination, a browser tier), which
    makes verifying one adapter change unreasonably slow.
    """
    from app.services.fetch_lock import state

    wanted = [s for s in sources if s in TRIGGERABLE_SOURCES]
    current = state()
    if current.get("running"):
        return templates.TemplateResponse(
            "runs/partials/status.html",
            {"request": request, "fetch_state": current,
             "triggered": {"ok": False,
                           "message": "A fetch is already running — wait for it to finish."}},
        )

    try:
        from app.tasks.fetch import fetch_jobs
        # Matching costs LLM calls; skip it for a narrow source test.
        fetch_jobs.delay(only=wanted or None, match_after=not wanted)
    except Exception as exc:
        logger.error("runs: could not queue fetch: %s", exc)
        return templates.TemplateResponse(
            "runs/partials/status.html",
            {"request": request, "fetch_state": current,
             "triggered": {"ok": False,
                           "message": f"Could not queue the fetch: {exc}"}},
        )

    scope = ", ".join(wanted) if wanted else "all sources"
    return templates.TemplateResponse(
        "runs/partials/status.html",
        {"request": request, "fetch_state": {"running": True, "seconds_left": None},
         "triggered": {"ok": True, "message": f"Fetch queued for {scope}."}},
    )
