import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/runs", tags=["runs"])
templates = Jinja2Templates(directory="app/templates")

# Enough to see a trend without turning the page into a wall.
DEFAULT_RUNS_SHOWN = 15
ROLLUP_WINDOW = 20

# NIM models worth comparing for job matching. Instruct-tuned ones first: the
# reasoning models below them wrap their answer in thinking, which the parser
# now survives but which still costs tokens and can hit the 512-token ceiling.
NIM_MODELS = [
    "meta/llama-3.3-70b-instruct",
    "meta/llama-3.1-70b-instruct",
    "qwen/qwen3-next-80b-a3b-instruct",
    "mistralai/mistral-medium-3.5-128b",
    "google/gemma-4-31b-it",
    "nvidia/llama-3.3-nemotron-super-49b-v1.5",
    "meta/llama-3.1-8b-instruct",
    "openai/gpt-oss-120b",
    "nvidia/nemotron-3-super-120b-a12b",
    "deepseek-ai/deepseek-v4-flash",
]

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
            **{k: v for k, v in _compare_context(request, db).items()
               if k != "request"},
        },
    )


def _compare_context(request: Request, db: Session, queued: dict | None = None) -> dict:
    """State for the model-comparison panel."""
    from app.models.profile import Profile
    from app.services.fetch_lock import COMPARE_LOCK_KEY, state

    profile = db.query(Profile).first()
    result = (profile.data.get("model_comparison") if profile else None) or None
    return {
        "request": request,
        "compare_state": state(key=COMPARE_LOCK_KEY),
        "compare_result": result,
        "compare_models_available": NIM_MODELS,
        "current_model": settings.NVIDIA_NIM_MODEL,
        "queued": queued,
    }


@router.get("/compare/status", response_class=HTMLResponse)
def compare_status(request: Request, db: Session = Depends(get_db)):
    """Live comparison state; the panel polls this while one is running."""
    return templates.TemplateResponse(
        "runs/partials/compare.html", _compare_context(request, db)
    )


@router.post("/compare", response_class=HTMLResponse)
def trigger_compare(request: Request, models: list[str] = Form(default=[]),
                    limit: int = Form(default=10), db: Session = Depends(get_db)):
    """
    Queue a model comparison.

    Two models is the useful case — the report highlights which jobs cross the
    accept threshold between the first two, which is the only difference that
    changes what you actually see.
    """
    from app.services.fetch_lock import COMPARE_LOCK_KEY, state

    wanted = [m for m in models if m in NIM_MODELS]
    if len(wanted) < 2:
        return templates.TemplateResponse(
            "runs/partials/compare.html",
            _compare_context(request, db, {
                "ok": False, "message": "Pick at least two models to compare."}),
        )

    if state(key=COMPARE_LOCK_KEY).get("running"):
        return templates.TemplateResponse(
            "runs/partials/compare.html",
            _compare_context(request, db, {
                "ok": False, "message": "A comparison is already running."}),
        )

    limit = max(1, min(limit, 50))
    try:
        from app.tasks.compare_models import run_comparison
        run_comparison.delay(models=wanted, limit=limit)
    except Exception as exc:
        logger.error("runs: could not queue comparison: %s", exc)
        return templates.TemplateResponse(
            "runs/partials/compare.html",
            _compare_context(request, db, {
                "ok": False, "message": f"Could not queue it: {exc}"}),
        )

    context = _compare_context(request, db, {
        "ok": True,
        "message": f"Comparing {len(wanted)} models over {limit} jobs — "
                   f"this takes a few minutes.",
    })
    context["compare_state"] = {"running": True, "seconds_left": None}
    return templates.TemplateResponse("runs/partials/compare.html", context)


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
