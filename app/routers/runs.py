import logging

from fastapi import APIRouter, Depends, Request
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

    return templates.TemplateResponse(
        "runs/index.html",
        {
            "request": request,
            "runs": runs,
            "source_totals": totals,
            "rollup_window": ROLLUP_WINDOW,
            "top_boards": boards,
        },
    )
