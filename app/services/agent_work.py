"""
Producing work for the browser, and doing something with what comes back.

The first real use of the agent queue. Aggregators link to their own redirect
page rather than the employer, and following those from the VPS is exactly the
request that gets a datacenter IP blocked — Indeed and Glassdoor in particular
serve an interstitial or a challenge rather than a redirect. The user's own
browser is not blocked, because it is a real browser on a residential
connection with the sessions to match.

So `link_resolver` still runs first and gets most of them cheaply. What it
could not follow becomes a `resolve_link` task, and the answer arrives whenever
the laptop is next awake. Nothing waits for it: the job is already saved, and an
apply URL that improves an hour later is strictly better than one that never
arrives.

Results come back through `browser_tasks.complete`, which is why ingestion lives
here rather than in the router — the same handler runs whichever engine did the
work, and a new task kind is a new entry in one dict.
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_

from app.config import settings
from app.models.browser_task import BrowserTask
from app.models.job import Job

logger = logging.getLogger(__name__)

# Coarse SQL prefilter for interstitial URLs. `link_resolver.is_interstitial` is
# the real test, but it is a set of Python regexes and cannot be pushed into the
# query — so this narrows the scan to plausible rows and the precise check runs
# on those. Deliberately over-inclusive: a false positive here costs one regex,
# a false negative costs a job that never gets resolved.
_INTERSTITIAL_SQL_HINTS = (
    "%adzuna%", "%jooble%", "%careerjet%", "%indeed.com%",
    "%appcast.io%", "%recruitics.com%", "%click.jobvite.com%",
    "%clickcast.jobs%", "%jobs2web.com%",
    "%/redirect?%", "%/out?url=%",
)

# How long a resolution attempt stands before the same URL is queued again.
# Long enough that a genuinely dead link is not retried every cycle.
_RETRY_AFTER_DAYS = 7


def _max_queued() -> int:
    return max(0, int(getattr(settings, "AGENT_LINK_RESOLVE_MAX_QUEUED", 100)))


# ---------------------------------------------------------------------------
# Producing
# ---------------------------------------------------------------------------

def enqueue_unresolved_links(db, limit: int | None = None) -> int:
    """
    Queue browser resolution for interstitials the server could not follow.

    Works from saved jobs rather than the in-flight fetch batch, which makes it
    idempotent and self-healing: it picks up anything still unresolved from
    earlier cycles, including from before an agent existed. Safe to call every
    cycle whether or not anyone is listening.
    """
    from app.services.link_resolver import is_interstitial

    budget = _max_queued() if limit is None else max(0, limit)
    if not budget:
        return 0

    candidates = (
        db.query(Job.url)
        .filter(
            Job.apply_url.is_(None),
            or_(*[Job.url.ilike(hint) for hint in _INTERSTITIAL_SQL_HINTS]),
        )
        .distinct()
        # Scan a bounded window. Ordering by url keeps it deterministic, and
        # anything missed this cycle is picked up on the next one.
        .limit(budget * 10)
        .all()
    )
    urls = [row[0] for row in candidates if is_interstitial(row[0] or "")]
    if not urls:
        return 0

    # Skip anything already in flight, and anything tried recently — a link that
    # 404s will 404 again, and requeueing it every cycle would crowd out work
    # that might actually succeed.
    cutoff = datetime.now(timezone.utc) - timedelta(days=_RETRY_AFTER_DAYS)
    seen = {
        row[0]
        for row in db.query(BrowserTask.payload["url"].astext)
        .filter(
            BrowserTask.kind == "resolve_link",
            or_(
                BrowserTask.status.in_(("queued", "leased")),
                BrowserTask.created_at >= cutoff,
            ),
        )
        .all()
    }

    from app.services import browser_tasks

    queued = 0
    for url in urls:
        if queued >= budget:
            break
        if url in seen:
            continue
        browser_tasks.enqueue(db, "resolve_link", {"url": url})
        queued += 1

    if queued:
        logger.info("agent_work: queued %d link(s) for browser resolution", queued)
    return queued


# ---------------------------------------------------------------------------
# Consuming
# ---------------------------------------------------------------------------

def enqueue_reddit_search(db, company: str) -> int:
    """
    Ask the browser for what Reddit refuses to give this server.

    Reddit answers a datacenter IP with `403 Blocked` — a categorical refusal,
    not a rate limit, so retrying from here never works. The browser is not
    blocked, because it is a browser on a home connection. This is the queue
    doing exactly what it was built for.
    """
    from app.services import browser_tasks
    from app.services.interview_sources import reddit_search_urls

    urls = reddit_search_urls(company)
    if not urls:
        return 0

    for url in urls:
        browser_tasks.enqueue(
            db,
            "fetch_json",
            {"url": url, "purpose": "interview_reddit", "company": company},
            # Above link resolution: somebody is waiting on this, having just
            # pressed a button, where link resolution is background tidying.
            priority=5,
            # A search is worth redoing tomorrow if today's browser was closed,
            # but not worth carrying for a week.
            ttl_hours=12,
        )
    logger.info("agent_work: queued %d Reddit search(es) for %s", len(urls), company)
    return len(urls)


def _ingest_fetch_json(db, task: BrowserTask) -> None:
    """
    Store whatever the browser fetched on our behalf.

    `purpose` in the payload decides how to read it, so one task kind serves
    every source the server is walled out of rather than needing a new kind —
    and the parsing is the same function the direct path uses, since only the
    thing that made the request differed.
    """
    from app.services.interview_corpus import ingest
    from app.services.interview_sources import parse_reddit

    payload = task.payload or {}
    result = task.result or {}
    purpose = payload.get("purpose")

    if purpose != "interview_reddit":
        return

    body = result.get("json")
    if body is None:
        _note(db, task, {"error": "the browser did not get JSON back"})
        return

    # Counted before and after filtering, because "the search found nothing" and
    # "the filter rejected everything" are the same zero from outside — and
    # they were, on the first live run, where a filter that was too strict
    # looked exactly like an agent that never ran.
    seen = len(((body.get("data") or {}).get("children") or [])) if isinstance(body, dict) else 0
    reports = parse_reddit(body, payload.get("company") or "")
    counts = ingest(db, reports) if reports else {"stored": 0, "duplicate": 0}

    _note(db, task, {
        "posts_seen": seen,
        "kept": len(reports),
        "stored": counts.get("stored", 0),
        "duplicate": counts.get("duplicate", 0),
        "via": result.get("via") or "fetch",
    })
    logger.info(
        "agent_work: reddit for %s — %d posts, %d kept, %d new",
        payload.get("company"), seen, len(reports), counts.get("stored", 0),
    )


def _note(db, task: BrowserTask, summary: dict) -> None:
    """
    Record what ingestion made of a result, on the task itself.

    A task that succeeded and yielded nothing is otherwise indistinguishable
    from one that never ran, which is the failure this whole subsystem keeps
    producing. Writing the outcome next to the result makes the difference
    readable on a page.
    """
    merged = dict(task.result or {})
    # The raw body has served its purpose and is large; the summary is what
    # anyone will ever look at.
    merged.pop("json", None)
    merged["ingest"] = summary
    task.result = merged
    db.commit()


def _ingest_resolve_link(db, task: BrowserTask) -> None:
    """
    Store the apply URL a browser found, and mine the page for ATS boards.

    Two independent wins, and the second survives the first failing: landing on
    another aggregator is not an apply link, but that page may still name the
    company's Greenhouse or Lever board, which is worth having either way.
    """
    from app.services.ats_discovery import discover_ats_slugs
    from app.services.link_resolver import is_aggregator
    from app.models.profile import Profile

    original = (task.payload or {}).get("url") or ""
    result = task.result or {}
    final_url = (result.get("final_url") or "").strip()
    html = result.get("html") or ""

    if not original or not final_url:
        return

    updated = 0
    if final_url != original and not is_aggregator(final_url):
        updated = (
            db.query(Job)
            .filter(Job.url == original, Job.apply_url.is_(None))
            .update({"apply_url": final_url}, synchronize_session=False)
        )
        if updated:
            logger.info(
                "agent_work: apply URL for %d job(s) resolved in-browser to %s",
                updated, final_url,
            )

    # How it was got matters: a resolution that needed a real page load says the
    # aggregator is refusing background requests, which is worth seeing before
    # every link starts needing a window.
    _note(db, task, {
        "jobs_updated": updated,
        "landed_on": final_url[:120],
        "via": result.get("via") or "fetch",
    })

    # Feed the ATS flywheel. Shaped as a job dict because that is what discovery
    # reads, and it scans the URL and the page body for board links alike.
    profile = db.query(Profile).first()
    if profile is not None and (html or final_url):
        data = dict(profile.data or {})
        merged = discover_ats_slugs(
            [{"url": final_url, "description": html}], data.get("discovered_ats")
        )
        if merged != data.get("discovered_ats"):
            data["discovered_ats"] = merged
            profile.data = data

    db.commit()


# Result handlers by task kind. A kind with no entry is simply stored — `ping`
# has nothing to ingest, and a task whose only job was to run is complete when
# its result is recorded.
RESULT_HANDLERS = {
    "resolve_link": _ingest_resolve_link,
    "fetch_json": _ingest_fetch_json,
}


def ingest(db, task: BrowserTask) -> None:
    """
    Act on a completed task.

    Never raises. The agent has already done the work and reported it honestly,
    so a handler that fails must not turn that into a failed task — the result
    is recorded either way, and the ingestion problem is ours to see in the log.
    """
    handler = RESULT_HANDLERS.get(task.kind)
    if not handler:
        return
    try:
        handler(db, task)
    except Exception as exc:
        logger.error(
            "agent_work: ingesting %s result for task %s failed: %s",
            task.kind, task.id, exc,
        )
        db.rollback()
