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
    try:
        return await run_in_threadpool(
            _report, db, browser_tasks.fail, task_id, agent_id, error=error
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
