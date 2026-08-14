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

# NIM models worth comparing for job matching, current default first.
#
# Reasoning models spend tokens thinking before they answer. The parser handles
# the wrapping, and NIM_MATCH_MAX_TOKENS is now sized for it — with a ceiling
# that only fits the JSON they get truncated mid-object, which reads as the
# model being bad at scoring rather than as a budget that was too small.
NIM_MODELS = [
    "z-ai/glm-5.2",
    "deepseek-ai/deepseek-v4-flash",
    "meta/llama-3.3-70b-instruct",
    "meta/llama-3.1-70b-instruct",
    "qwen/qwen3-next-80b-a3b-instruct",
    "mistralai/mistral-medium-3.5-128b",
    "google/gemma-4-31b-it",
    "nvidia/llama-3.3-nemotron-super-49b-v1.5",
    "meta/llama-3.1-8b-instruct",
    "openai/gpt-oss-120b",
    "nvidia/nemotron-3-super-120b-a12b",
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
            "system": _system_context(db),
            **{k: v for k, v in _compare_context(request, db).items()
               if k != "request"},
        },
    )


def _system_context(db: Session) -> dict:
    """
    The state of the subsystems that have no page of their own.

    Each of these previously required shelling into a container to inspect,
    which meant nobody inspected them. Every lookup is wrapped: a panel that
    cannot render its own status should degrade to a note, not take down the
    fetch history it sits beside.
    """
    from app.services import browser_tasks, interview_corpus, mailbox

    context: dict = {
        "agent": None, "mailbox": None, "corpus": None, "pool": None, "errors": [],
    }

    try:
        context["agent"] = {
            "queue": browser_tasks.queue_stats(db),
            "recent": browser_tasks.recent(db, 8),
            "last_agent": browser_tasks.last_agent(db),
            "configured": bool((settings.AGENT_TOKEN or "").strip()),
        }
    except Exception as exc:
        logger.warning("runs: agent status unavailable: %s", exc)
        context["errors"].append(f"agent queue: {exc}")

    try:
        from app.database import pool_status

        context["pool"] = pool_status()
    except Exception as exc:
        logger.warning("runs: pool status unavailable: %s", exc)

    try:
        context["mailbox"] = mailbox.status(db)
    except Exception as exc:
        logger.warning("runs: mailbox status unavailable: %s", exc)
        context["errors"].append(f"mailbox: {exc}")

    try:
        context["corpus"] = interview_corpus.coverage(db)
    except Exception as exc:
        logger.warning("runs: corpus coverage unavailable: %s", exc)
        context["errors"].append(f"interview corpus: {exc}")

    return context


@router.post("/agent/ping", response_class=HTMLResponse)
def queue_ping(request: Request, db: Session = Depends(get_db)):
    """
    Queue a ping task, so the extension can be tested from a page.

    `ping` depends on nothing — no site reachable, no session valid — so a
    completed one proves the whole round trip and a stuck one narrows the
    problem to the agent rather than to whatever it was asked to do.
    """
    from app.services import browser_tasks

    try:
        browser_tasks.enqueue(db, "ping", {"from": "runs page"})
    except Exception as exc:
        logger.error("runs: could not queue ping: %s", exc)
    return templates.TemplateResponse(
        "runs/_system.html", {"request": request, "system": _system_context(db)}
    )


@router.get("/system", response_class=HTMLResponse)
def system_status(request: Request, db: Session = Depends(get_db)):
    """The system panel on its own, for polling while a task is in flight."""
    return templates.TemplateResponse(
        "runs/_system.html", {"request": request, "system": _system_context(db)}
    )


def _compare_context(request: Request, db: Session, queued: dict | None = None) -> dict:
    """
    State for the model-comparison panel.

    Liveness comes from the stored record, not from the Redis lock: the lock is
    only held while a worker is executing, so between the click and the worker
    picking the task up "not running" and "never asked for" look identical —
    the panel would stop polling and show nothing at all.
    """
    from app.services.model_compare import load_state, progress

    try:
        record = load_state(db)
    except Exception as exc:
        logger.warning("runs: comparison state unavailable: %s", exc)
        record = None

    return {
        "request": request,
        "compare_result": record,
        "compare_progress": progress(record),
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
    from app.services.model_compare import (
        load_state, mark_queued, progress, store_state,
    )

    def panel(message: str, ok: bool = False):
        return templates.TemplateResponse(
            "runs/partials/compare.html",
            _compare_context(request, db, {"ok": ok, "message": message}),
        )

    wanted = [m for m in models if m in NIM_MODELS]
    if len(wanted) < 2:
        return panel("Pick at least two models to compare.")

    if progress(load_state(db))["active"]:
        return panel("A comparison is already queued or running.")

    limit = max(1, min(limit, 50))
    # Recorded before the task is published so the panel has something to show
    # the moment it swaps in, rather than between then and the worker starting.
    mark_queued(db, wanted, limit)
    try:
        from app.tasks.compare_models import run_comparison
        run_comparison.delay(models=wanted, limit=limit)
    except Exception as exc:
        logger.error("runs: could not queue comparison: %s", exc)
        # Clear the queued record, or the panel polls forever for a task that
        # was never published.
        store_state(db, {"status": "failed", "error": f"could not queue it: {exc}",
                         "models": wanted, "job_count": limit,
                         "rows": [], "summary": [], "flips": []})
        return panel(f"Could not queue it: {exc}")

    return templates.TemplateResponse(
        "runs/partials/compare.html",
        _compare_context(request, db, {
            "ok": True,
            "message": f"Queued: {len(wanted)} models over {limit} jobs. "
                       f"That's {len(wanted) * limit} LLM calls, so give it a "
                       f"few minutes — this panel updates itself.",
        }),
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
