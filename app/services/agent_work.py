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
