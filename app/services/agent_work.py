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


def _ingest_enrichment(db, task: BrowserTask, payload: dict, final_url: str,
                       html: str) -> None:
    """
    Read the description out of a page the browser fetched for us.

    LinkedIn and Dice answer this server with a challenge and a real browser
    with the posting, so for those hosts this is the only path to a description
    at all — 8,800 stored LinkedIn jobs have none. The extraction is the same
    code the server-side pass runs; only the thing that made the request
    differed.
    """
    from app.services.enrichment import apply_extraction, extract_from_html

    job_id = payload.get("job_id")
    job = db.query(Job).filter(Job.id == job_id).first() if job_id else None
    if job is None:
        _note(db, task, {"error": "the job this was fetched for is gone"})
        return

    if not html:
        _note(db, task, {"enriched": False, "reason": "no page body came back",
                         "landed_on": final_url[:120]})
        return

    found = extract_from_html(html, job_id=job.id)
    outcome = apply_extraction(db, job, found)

    # An apply URL is worth taking from this trip too, since the browser
    # followed the whole chain to get here.
    from app.services.job_edits import is_manual
    from app.services.link_resolver import is_aggregator

    if final_url and final_url != payload.get("url") and not is_aggregator(final_url):
        if not job.apply_url and not is_manual(job, "apply_url"):
            job.apply_url = final_url

    db.commit()
    _note(db, task, {
        "enriched": outcome["improved"],
        "chars_gained": outcome["chars_gained"],
        "requeued_for_matching": outcome["requeued"],
        "method": found.method or "none",
        "via": (task.result or {}).get("via") or "fetch",
    })
    if outcome["improved"]:
        logger.info(
            "agent_work: enriched %s in-browser (+%d chars via %s)",
            job.id, outcome["chars_gained"], found.method,
        )


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

    payload = task.payload or {}
    original = payload.get("url") or ""
    result = task.result or {}
    final_url = (result.get("final_url") or "").strip()
    html = result.get("html") or ""

    if not original or not final_url:
        # Used to return in silence, which put the task on the panel as "done"
        # with nothing beside it — the exact reading this whole subsystem keeps
        # producing, where a task that yielded nothing is indistinguishable
        # from one that never ran.
        _note(db, task, {
            "error": "no URL came back"
            if original else "the task had no URL to resolve",
        })
        return

    # Queued by enrichment rather than by link resolution: the page it brought
    # back is the whole point, not the URL it landed on. Handled first because
    # it names the exact job it was fetched for.
    if payload.get("purpose") == "enrich":
        _ingest_enrichment(db, task, payload, final_url, html)
        return

    updated = 0
    if final_url != original and not is_aggregator(final_url):
        # `apply_url IS NULL` already spares a job whose link the user typed —
        # except for one they deliberately cleared, which is a statement that
        # the resolved link was wrong. The array check honours that too.
        updated = (
            db.query(Job)
            .filter(
                Job.url == original,
                Job.apply_url.is_(None),
                ~Job.manual_fields.any("apply_url"),
            )
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


def _ingest_browse_page(db, task: BrowserTask) -> None:
    """
    Record that a page was visited. The jobs came in by another door.

    There is nothing to store from the result itself: the interceptor read the
    page's own API responses while it was open and posted them to `/harvest`,
    which saved whatever was in them long before this ran. What is worth
    keeping is whether the visit was real — a login wall renders instead of the
    posting, so the harvest finds nothing and that looks exactly like a reader
    whose field names moved. Distinguishing the two is the entire value here.
    """
    from app.services import agent_events

    result = task.result or {}
    payload = task.payload or {}
    signed_in = result.get("signed_in", True)
    # "" when the page never asked; otherwise passed / timeout / skipped.
    challenge = str(result.get("challenge") or "")
    blocked = challenge in ("timeout", "skipped")
    # The board asked us to wait. Not a failure of the visit — the pages
    # harvested before the limit are real and already stored — but the reason
    # the visit ended, and the thing the next one has to be shaped around.
    rate_limited = bool(result.get("rate_limited"))
    passes_done = int(result.get("passes_done") or 0)
    # Result pages reached by clicking through. On a board the server asked to
    # paginate, a 1 means the "next" control was not found — the board redesigned
    # its pagination, or the guess at its markup was wrong. That is a specific
    # and fixable thing, and it is invisible in every other number here: the
    # scroll looks healthy, the harvest returns rows, and page one is all you
    # ever get.
    pages_done = int(result.get("pages_done") or 0)

    agent_events.record(
        db, "browse", url=result.get("final_url") or payload.get("url"),
        agent_id=task.agent_id or "",
        # A page we never got past is not a successful visit. Counting it as
        # one is how a site that blocks every request shows up on the panel as
        # a reader whose field names moved — and sends you to fix the parser
        # for a page you never saw.
        ok=bool(signed_in) and not blocked,
        summary={
            "purpose": payload.get("purpose") or "harvest",
            "signed_in": bool(signed_in),
            "challenge": challenge,
            "rate_limited": rate_limited,
            "pages_done": pages_done,
            # How deep the scroll got. When the visit ended in a limit this is
            # the depth this board tolerated today, which is what the next
            # visit's depth is built from.
            "passes_done": passes_done,
            "title": str(result.get("title") or "")[:200],
            # How far down the list the scroll got. On a board that scrolls
            # infinitely this is the only measure of whether the visit walked
            # the page or gave up on the first stall — and the scroll is the
            # pagination there, so a small number means a shallow crawl.
            "scrolled_px": int(result.get("scrolled_px") or 0),
            # How many times new content actually arrived. The honest measure
            # of whether the scroll worked: pixels can move on a page that
            # loads nothing, and on an infinite-scroll board one batch means
            # the crawl saw the first screen and stopped.
            "batches": int(result.get("batches") or 0),
            # Which element got scrolled, so a wrong guess about where the
            # list lives is diagnosable instead of invisible.
            "scroll_target": str(result.get("scroll_target") or "")[:80],
        },
    )
    db.commit()

    from app.services import browse_plan

    asked_pages = browse_plan._max_pages(payload.get("url") or "")
    if asked_pages > 1 and pages_done <= 1 and not rate_limited:
        logger.warning(
            "agent_work: %s was asked for %d pages and reached %d — the "
            "next-page control was not found, so only the first page was "
            "harvested",
            payload.get("url"), asked_pages, pages_done,
        )
    if rate_limited:
        logger.info(
            "agent_work: %s asked us to slow down after %d scroll pass(es) — "
            "resting that host and going shallower next time",
            payload.get("url"), passes_done,
        )
    if blocked:
        logger.warning(
            "agent_work: %s asked for a human check and did not get past it "
            "(%s) — backing off that host",
            payload.get("url"), challenge,
        )
    elif not signed_in:
        logger.warning(
            "agent_work: %s rendered a sign-in wall rather than the posting — "
            "the browsing session is logged out",
            payload.get("url"),
        )


# Result handlers by task kind. A kind with no entry is simply stored — `ping`
# has nothing to ingest, and a task whose only job was to run is complete when
# its result is recorded.
RESULT_HANDLERS = {
    "resolve_link": _ingest_resolve_link,
    "fetch_json": _ingest_fetch_json,
    "browse_page": _ingest_browse_page,
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
