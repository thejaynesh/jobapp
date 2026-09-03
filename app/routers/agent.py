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
from datetime import datetime, timezone

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

# Boards this server knows how to sweep on its own, and therefore the only ones
# worth being handed a credential for. A name not on this list would store
# something nothing ever reads, and report success for it.
LINKABLE_SITES = frozenset({"tsenta"})


def _bad_request(exc: "TaskError | str") -> JSONResponse:
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

    `lease_seconds` rides along with the work, so an agent can pace its
    heartbeats off the server's actual setting rather than a number copied into
    its own source. A batch is leased at one instant and worked through one task
    at a time, so the agent has to hold the leases open itself; it can only do
    that if it knows how long it has.
    """
    body = await _json_body(request)
    kinds = [str(k) for k in (body.get("kinds") or []) if str(k).strip()]
    agent_id = str(body.get("agent_id") or "")[:120]
    harvest_sites = [
        str(h).strip().lower()[:160]
        for h in (body.get("harvest_sites") or [])
        if str(h).strip()
    ][:60]
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
        await run_in_threadpool(
            browser_tasks.record_agent_seen, db, agent_id, kinds, harvest_sites,
        )
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
            return {"tasks": tasks, "lease_seconds": browser_tasks._lease_seconds()}
        if time.monotonic() >= deadline:
            return {"tasks": [], "lease_seconds": browser_tasks._lease_seconds()}
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)


def _harvest(db: Session, payload, source_url: str = "", agent_id: str = "",
             probe: bool = False) -> dict:
    from app.services import agent_events, harvest_recipes, harvest_samples
    from app.services.harvest import extract_jobs, save_harvested_jobs, source_for_url

    # The page it came off decides the source name. The extractor is
    # shape-based and host-agnostic, so without this every site's yield would
    # be filed under LinkedIn and none of them could be judged separately.
    source = source_for_url(source_url)
    host = agent_events.host_of(source_url) or ""

    # A learned recipe first, the generic walker as the fallback — never the
    # other way round. The walker is the thing that works everywhere, so a
    # recipe may add a way to read a site and must not be able to remove one.
    read_by = "walker"
    jobs = []
    recipe = harvest_recipes.active_for(db, host)
    if recipe:
        jobs = harvest_recipes.apply_recipe(payload, recipe, source)
        if jobs:
            read_by = "recipe"
    if not jobs:
        jobs = extract_jobs(payload, source=source)

    if not jobs:
        counts = {"found": 0, "inserted": 0, "merged": 0, "skipped": 0, "invalid": 0}
        # Keep the payload. Until now it was discarded here, which left
        # "forwarding, never finds jobs" a verdict with no evidence attached
        # and no way to act on it short of opening DevTools by hand.
        harvest_samples.record(
            db, host, payload, source_url=source_url, found=0,
            note=(
                # A probe named none of the keys the reader looks for, which is
                # a different thing from a payload that did and still yielded
                # nothing — the first wants new field names, the second wants a
                # recipe, and they read identically without this.
                "near miss: no field names the reader knows"
                if probe else
                ("recipe found nothing" if recipe else "no recipe")
            ),
        )
    else:
        counts = {"found": len(jobs), "source": source, **save_harvested_jobs(db, jobs)}

    # A harvest that found nothing is the single most common outcome and used
    # to leave no trace at all, so "the interceptor is forwarding rubbish" and
    # "the extension is not running" looked identical.
    agent_events.record(
        db, "harvest", url=source_url, agent_id=agent_id,
        ok=True, summary={"source": source, "read_by": read_by, **counts},
    )
    db.commit()
    return counts


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
            _harvest, db, payload, body.get("source_url") or "",
            str(body.get("agent_id") or "")[:120],
            bool(body.get("probe")),
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


@router.post("/link")
async def link_account(request: Request, db: Session = Depends(get_db)):
    """
    A board's own credential, handed over so the server can call its API.

    Body: {"site": "tsenta", "api_key": "...", "refresh_token": "..."}

    The one thing the browser has and the server cannot get for itself. Tsenta's
    API takes a Firebase ID token that lasts an hour; the refresh token behind
    it lasts until it is revoked, and Google's public `securetoken` endpoint
    mints fresh ID tokens from it. So this is what turns a sweep that needs a
    laptop open into one that runs on a schedule.

    Sent on every visit to the board rather than once. That is the repair path:
    a credential that has gone stale is fixed by the user opening the site, and
    nobody has to know that is the fix.

    Only sites this server actually knows how to sweep are accepted — not as a
    gate on anything, but because a typo'd site name would store a credential
    that nothing would ever read and report success for it.
    """
    from app.services import linked_auth

    body = await _json_body(request)
    site = str(body.get("site") or "").strip().lower()[:40]
    api_key = str(body.get("api_key") or "").strip()[:200]
    refresh_token = str(body.get("refresh_token") or "").strip()

    if site not in LINKABLE_SITES:
        return _bad_request(
            f"Unknown site {site!r}. Known: {', '.join(sorted(LINKABLE_SITES))}."
        )
    if not api_key or not refresh_token:
        return _bad_request("Both api_key and refresh_token are required.")

    try:
        row = await run_in_threadpool(
            linked_auth.link, db, site, api_key, refresh_token
        )
    except Exception as exc:
        logger.error("agent: could not store the %s credential: %s", site, exc)
        return JSONResponse({"detail": "Could not store it."}, status_code=500)

    # Deliberately not echoed back, not logged, and not returned: there is
    # nothing the caller learns from seeing its own token again.
    logger.info("agent: linked %s", site)
    return {"ok": True, "site": site, "linked_at": row.linked_at.isoformat()}


@router.post("/report")
async def report_events(request: Request, db: Session = Depends(get_db)):
    """
    Events the extension saw, which the server otherwise never learns about.

    Body: {"agent_id": "...", "events": [{"kind", "url"|"host", "ok",
    "summary"}, ...]}

    Autofill outcomes, overlay lookups, resume attachments — all of them happen
    entirely in the browser and end there. That is why "the extension is not
    working" has never been answerable: the only things this server saw were
    the calls the extension chose to make, and a fill that recognised nothing
    makes no calls at all.

    Forgiving on purpose. The client buffers these while the server is
    unreachable and posts the backlog on reconnect, so a malformed entry costs
    that entry rather than the batch, and an unknown kind is filed rather than
    rejected — an extension newer than the server still leaves a trace.
    """
    from app.services import agent_events

    body = await _json_body(request)
    events = body.get("events")
    if events is None and body.get("kind"):
        # A single event posted bare. Cheap to accept and one less shape for
        # the client to get wrong.
        events = [body]
    return await run_in_threadpool(
        agent_events.record_batch, db, events, str(body.get("agent_id") or "")[:120]
    )


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

    from app.services import screening

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
        # The five questions every form asks and nobody enjoys retyping. Blank
        # when unset, and blank is left blank: these are declarations going to
        # an employer, and a guessed answer is worse than an empty box because
        # the empty box gets noticed.
        **screening.answers(data),
    }


@router.get("/autofill-fields")
async def autofill_fields(db: Session = Depends(get_db)):
    """
    What to put in an application form.

    Fetched on demand when the user presses Fill, not on page load, so profile
    values reach a page only when they have asked for them to be typed there.
    """
    return await run_in_threadpool(_autofill_fields, db)


def _resume(db: Session, url: str) -> dict:
    """
    The current resume for the posting on screen, as bytes the page can attach.

    A content script *can* fill a file input — build a `File`, put it in a
    `DataTransfer`, assign `input.files` — but it cannot get the bytes: the PDF
    lives behind the agent token, which is held by the background worker and
    has no business being handed to an employer's page. So the worker fetches
    it here and passes it through.

    Base64 in JSON rather than a binary response because that is what survives
    `chrome.runtime.sendMessage`, which cannot carry a Blob.
    """
    import base64
    from pathlib import Path

    from app.models.application import Application, ApplicationDocument, DocType
    from app.services.job_context import find_job

    job = find_job(db, url)
    if job is None:
        return {"ok": False, "detail": "This posting isn't in your tracker yet."}

    application = (
        db.query(Application)
        .filter(Application.job_id == job.id)
        .order_by(Application.created_at.desc())
        .first()
    )
    if application is None:
        return {"ok": False, "detail": "No application for this job yet."}

    document = (
        db.query(ApplicationDocument)
        .filter(
            ApplicationDocument.application_id == application.id,
            ApplicationDocument.doc_type == DocType.resume,
            ApplicationDocument.is_current.is_(True),
        )
        .order_by(ApplicationDocument.created_at.desc())
        .first()
    )
    if document is None:
        return {"ok": False, "detail": "No resume has been written for this yet."}

    path = Path(document.path)
    if not path.exists():
        # The row outlives the file when a volume is remounted or a container
        # rebuilt. Saying which is the difference between a fixable problem
        # and a mysterious one.
        logger.warning("agent: resume row %s points at a missing file %s",
                       document.id, path)
        return {"ok": False, "detail": "The resume file is missing on the server."}

    data = path.read_bytes()
    # A name the employer sees. The stored filename is a UUID and a version.
    company = "".join(
        ch for ch in (job.company or "") if ch.isalnum() or ch in " -_"
    ).strip().replace(" ", "_")
    return {
        "ok": True,
        "filename": f"resume_{company}.pdf" if company else "resume.pdf",
        "content_type": "application/pdf",
        "size": len(data),
        "data": base64.b64encode(data).decode("ascii"),
    }


@router.get("/resume")
async def resume_for_posting(url: str = "", db: Session = Depends(get_db)):
    """The current resume for this posting, for the overlay to attach."""
    if not url.strip():
        return JSONResponse({"detail": "No url."}, status_code=400)
    return await run_in_threadpool(_resume, db, url)


def _mark_applied(db: Session, url: str) -> dict:
    """
    Record that this posting was applied to, from the page it was applied on.

    The moment the user presses Submit on the employer's form is the only
    moment they know for certain that they applied — and it is the moment they
    are furthest from the tracker. Every application marked days later, or not
    at all, is this gap.

    Idempotent: pressing it twice is the obvious thing to do when you are not
    sure whether the first press worked, and it must not move `applied_at`
    backwards or forwards on the second try.
    """
    from app.models.application import Application, ApplicationStatus
    from app.services.job_context import find_job

    job = find_job(db, url)
    if job is None:
        return {"ok": False, "detail": "This posting isn't in your tracker yet."}

    application = (
        db.query(Application)
        .filter(Application.job_id == job.id)
        .order_by(Application.created_at.desc())
        .first()
    )
    if application is None:
        return {"ok": False, "detail": "No application for this job yet."}

    if application.status != ApplicationStatus.not_applied:
        return {
            "ok": True,
            "status": application.status.value,
            "changed": False,
            "detail": f"Already marked {application.status.value.replace('_', ' ')}.",
        }

    application.status = ApplicationStatus.applied
    application.applied_at = datetime.now(timezone.utc)
    db.commit()
    logger.info("agent: marked application %s applied from the overlay",
                application.id)
    return {"ok": True, "status": "applied", "changed": True}


@router.post("/mark-applied")
async def mark_applied(request: Request, db: Session = Depends(get_db)):
    """Mark the posting at this URL as applied. Body: {"url": "..."}"""
    body = await _json_body(request)
    url = str(body.get("url") or "").strip()
    if not url:
        return JSONResponse({"detail": "No url."}, status_code=400)
    return await run_in_threadpool(_mark_applied, db, url)


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
