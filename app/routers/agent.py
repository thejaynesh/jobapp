"""
The agent protocol: /api/agent/*.

Authentication is not handled here. The middleware in `app.main` gates this
whole prefix on the `AGENT_TOKEN` bearer, which is deliberate — a per-route
dependency protects the routes somebody remembered to decorate, and the failure
mode of forgetting one is an open endpoint into the queue.

The laptop long-polls: it asks for work and the request waits rather than
returning empty immediately, so a queued task starts within a second or two
instead of on the next poll tick. Two constraints shape how that is written.

The app runs sync SQLAlchemy under a small number of uvicorn workers, so a poll
that held a worker (or the event loop) for its whole duration would let a
handful of idle agents starve the web UI. The wait therefore happens in
`asyncio.sleep`, which costs nothing but a coroutine, and each database check is
a short trip through the threadpool. The request's session stays checked out for
the duration, which is affordable because the design tops out at three engines
on one laptop — not because connections are free.

The other constraint comes from the client: an MV3 service worker is terminated
after about 30 seconds idle, so the poll ceiling stays below that. An in-flight
fetch keeps the worker alive; a poll longer than the timeout would not.
"""

import asyncio
import logging
import time

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.browser_task import TASK_KINDS
from app.services import browser_tasks
from app.services.browser_tasks import TaskError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent", tags=["agent"])

# How often the poll re-checks the queue while waiting. Short enough to feel
# immediate, long enough that an idle agent is not a busy loop against Postgres.
_POLL_INTERVAL_SECONDS = 1.0


def _bad_request(exc: TaskError) -> JSONResponse:
    return JSONResponse({"detail": str(exc)}, status_code=400)


async def _json_body(request: Request) -> dict:
    """
    The request body, tolerating an empty one.

    Agents post `fail` with no body when they have nothing to say beyond the
    fact of failure, and a 422 there is a worse answer than an empty dict.
    """
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


@router.get("/hello")
async def hello(db: Session = Depends(get_db)):
    """
    Handshake. Confirms the token works and reports what this server speaks.

    The options page calls this behind its "Test connection" button, so the
    response is shaped for a human reading it there: the queue depth is the
    thing you actually want to see when wondering whether the agent is idle
    because it is broken or because there is nothing to do.
    """
    stats = await run_in_threadpool(browser_tasks.queue_stats, db)
    return {
        "ok": True,
        "service": "jobapp",
        "protocol": 1,
        "kinds": list(TASK_KINDS),
        "lease_seconds": browser_tasks._lease_seconds(),
        "max_poll_seconds": _max_poll_seconds(),
        "queue": stats,
        "server_time": time.time(),
    }


def _max_poll_seconds() -> int:
    return max(0, int(getattr(settings, "AGENT_POLL_MAX_WAIT_SECONDS", 25)))


def _lease_once(db: Session, kinds: list[str], agent_id: str, limit: int) -> list[dict]:
    """One atomic attempt to claim work. Serialized inside the threadpool call
    so no ORM object escapes into the event loop still attached to a session."""
    tasks = browser_tasks.lease(db, kinds or None, agent_id=agent_id, limit=limit)
    return [task.as_dict() for task in tasks]


@router.post("/lease")
async def lease(request: Request, db: Session = Depends(get_db)):
    """
    Claim work, waiting up to `wait` seconds for some to appear.

    Body: {"kinds": [...], "agent_id": "...", "max": 1, "wait": 25}

    Returns `{"tasks": []}` on timeout rather than an error — an empty queue is
    the normal case, not a problem, and an agent should not have to distinguish
    "nothing to do" from "something went wrong" by reading a status code.
    """
    body = await _json_body(request)
    kinds = [str(k) for k in (body.get("kinds") or []) if str(k).strip()]
    agent_id = str(body.get("agent_id") or "")[:120]
    limit = body.get("max", 1)
    try:
        limit = max(1, int(limit))
    except (TypeError, ValueError):
        limit = 1

    try:
        wait = float(body.get("wait", _max_poll_seconds()))
    except (TypeError, ValueError):
        wait = _max_poll_seconds()
    wait = max(0.0, min(wait, _max_poll_seconds()))

    # Once per request, not per attempt: the poll re-checks the queue every
    # second and this is a write.
    try:
        await run_in_threadpool(browser_tasks.record_agent_seen, db, agent_id, kinds)
    except Exception as exc:
        # Presence is a diagnostic, not the job. Failing to note it must not
        # stop an agent that is asking for work from getting any.
        logger.warning("agent: could not record presence: %s", exc)

    deadline = time.monotonic() + wait
    while True:
        try:
            tasks = await run_in_threadpool(_lease_once, db, kinds, agent_id, limit)
        except TaskError as exc:
            return _bad_request(exc)

        if tasks:
            logger.info(
                "agent: leased %d task(s) to %s", len(tasks), agent_id or "anonymous"
            )
            return {"tasks": tasks}
        if time.monotonic() >= deadline:
            return {"tasks": []}
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)


def _harvest(db: Session, payload, source_url: str = "") -> dict:
    from app.services.harvest import extract_jobs, save_harvested_jobs, source_for_url

    # The page it came off decides the source name. The extractor is
    # shape-based and host-agnostic, so without this every site's yield would
    # be filed under LinkedIn and none of them could be judged separately.
    source = source_for_url(source_url)
    jobs = extract_jobs(payload, source=source)
    if not jobs:
        return {"found": 0, "inserted": 0, "merged": 0, "skipped": 0, "invalid": 0}
    counts = save_harvested_jobs(db, jobs)
    return {"found": len(jobs), "source": source, **counts}


@router.post("/harvest")
async def harvest(request: Request, db: Session = Depends(get_db)):
    """
    Job JSON the browser saw, offered rather than requested.

    A push rather than a task: nobody queued this, the user simply browsed a
    page and the extension read the response it was already receiving. So there
    is no task id to report against and no lease to hold.

    Body: {"payload": <the intercepted response>, "source_url": "..."}

    A payload with nothing job-shaped in it is a normal outcome, not an error —
    the interceptor is deliberately indiscriminate about what it forwards, on
    the grounds that guessing which endpoints matter is what rots.
    """
    body = await _json_body(request)
    payload = body.get("payload")
    if payload is None:
        return JSONResponse({"detail": "No payload."}, status_code=400)
    try:
        counts = await run_in_threadpool(
            _harvest, db, payload, body.get("source_url") or ""
        )
    except Exception as exc:
        # Never charge a parsing bug of ours to the browser that volunteered
        # the data; it has no way to act on the failure and nothing to retry.
        logger.error("agent: harvest ingestion failed: %s", exc)
        return {"found": 0, "error": "ingestion failed"}
    if counts["found"]:
        logger.info(
            "agent: harvested %d job(s) from %s",
            counts["found"], (body.get("source_url") or "an intercepted response")[:200],
        )
    return counts


@router.get("/job-context")
async def job_context(url: str = "", db: Session = Depends(get_db)):
    """
    What we already know about the posting the user is looking at.

    A GET with the URL as a parameter, because it is a read the overlay makes
    on every job page and caching it is the browser's business, not ours.
    """
    from app.services.job_context import context

    if not url.strip():
        return JSONResponse({"detail": "No url."}, status_code=400)
    return await run_in_threadpool(context, db, url)


def _autofill_fields(db: Session) -> dict:
    """
    The profile values worth typing into an application form, and nothing else.

    Deliberately a narrow projection rather than the whole profile. These end up
    in the page's own process — that is unavoidable, since typing them into a
    form is the point — so what travels is only what a form actually asks for.
    The narrative, the match scores, the application history and the LaTeX
    templates have no business on an employer's page.
    """
    from app.models.profile import Profile

    profile = db.query(Profile).first()
    data = (profile.data if profile else {}) or {}
    personal = data.get("personal") or {}

    name = (personal.get("name") or "").strip()
    first, _, last = name.partition(" ")

    education = (data.get("education") or [])
    latest = education[0] if education else {}

    return {
        "first_name": first,
        "last_name": last.strip(),
        "full_name": name,
        "email": (personal.get("email") or "").strip(),
        "phone": (personal.get("phone") or "").strip(),
        "location": (personal.get("location") or "").strip(),
        "linkedin": (personal.get("linkedin") or "").strip(),
        "github": (personal.get("github") or "").strip(),
        "website": (personal.get("website") or "").strip(),
        "school": (latest.get("school") or "").strip(),
        "degree": (latest.get("degree") or "").strip(),
        "field_of_study": (latest.get("field") or "").strip(),
    }


@router.get("/autofill-fields")
async def autofill_fields(db: Session = Depends(get_db)):
    """
    What to put in an application form.

    Fetched on demand when the user presses Fill, not on page load, so profile
    values reach a page only when they have asked for them to be typed there.
    """
    return await run_in_threadpool(_autofill_fields, db)


def _prepare(db: Session, url: str, posting: dict) -> dict:
    from app.services.job_context import prepare

    result = prepare(db, url, posting)
    if not result.get("ok"):
        return result

    # Only write documents for an application that did not already exist.
    # Re-queueing on a second click would overwrite a resume the user may have
    # already edited, which is not what a button called "generate" implies.
    if result.get("created_application"):
        try:
            from app.tasks.generate import generate_docs

            # retry=False because this is on a click path. The default publish
            # retry policy spends the better part of a minute on an unreachable
            # broker, which the person waiting on the button reads as a hang;
            # failing at once and saying so is the more honest answer.
            generate_docs.apply_async(
                args=[result["application_id"]], retry=False
            )
            result["generating"] = True
        except Exception as exc:
            # Celery being unreachable should not lose the application we just
            # opened; the app's own page can regenerate.
            logger.error("agent: could not queue generation: %s", exc)
            result["generating"] = False
    else:
        result["generating"] = False
    return result


@router.post("/prepare")
async def prepare_application(request: Request, db: Session = Depends(get_db)):
    """
    Take a posting from "on screen" to "documents being written".

    Body: {"url": "...", "posting": {title, company, location, description}}

    The posting details are a fallback for a job we have never fetched — the
    overlay reads them off the page it is already on, so a listing the pipeline
    never found is still one click from an application.
    """
    body = await _json_body(request)
    url = str(body.get("url") or "").strip()
    if not url:
        return JSONResponse({"detail": "No url."}, status_code=400)
    posting = body.get("posting")
    if not isinstance(posting, dict):
        posting = {}
    return await run_in_threadpool(_prepare, db, url, posting)


def _report(db: Session, action, task_id: str, agent_id: str, **kwargs) -> dict:
    task = action(db, task_id, agent_id=agent_id, **kwargs)
    return {"id": str(task.id), "status": task.status, "attempts": task.attempts}


@router.post("/tasks/{task_id}/result")
async def post_result(task_id: str, request: Request, db: Session = Depends(get_db)):
    """Report success. Body: {"result": {...}, "agent_id": "..."}"""
    body = await _json_body(request)
    result = body.get("result")
    if not isinstance(result, dict):
        result = {"value": result} if result is not None else {}
    agent_id = str(body.get("agent_id") or "")[:120]
    try:
        return await run_in_threadpool(
            _report, db, browser_tasks.complete, task_id, agent_id, result=result
        )
    except TaskError as exc:
        return _bad_request(exc)


@router.post("/tasks/{task_id}/fail")
async def post_failure(task_id: str, request: Request, db: Session = Depends(get_db)):
    """
    Report failure. Body: {"error": "...", "agent_id": "..."}

    The response says whether the task went back in the queue or was retired, so
    an agent can tell a retry from a dead end without querying for it.
    """
    body = await _json_body(request)
    agent_id = str(body.get("agent_id") or "")[:120]
    error = str(body.get("error") or "")[:2000]
    # The agent knows whether what it hit will change on a retry. A refused
    # request will be refused again; a timeout might not be.
    permanent = bool(body.get("permanent"))
    try:
        return await run_in_threadpool(
            _report, db, browser_tasks.fail, task_id, agent_id,
            error=error, permanent=permanent,
        )
    except TaskError as exc:
        return _bad_request(exc)


@router.post("/tasks/{task_id}/heartbeat")
async def post_heartbeat(task_id: str, request: Request, db: Session = Depends(get_db)):
    """Extend a lease on work still in progress."""
    body = await _json_body(request)
    agent_id = str(body.get("agent_id") or "")[:120]
    try:
        return await run_in_threadpool(
            _report, db, browser_tasks.heartbeat, task_id, agent_id
        )
    except TaskError as exc:
        return _bad_request(exc)
