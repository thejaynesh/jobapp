"""
Pages worth opening, so the harvest reads them without anybody visiting.

The interceptor already turns a page you look at into stored jobs. Its limit
was never extraction — it reads LinkedIn's own API responses and gets fuller
data than the guest API ever returns — it was *attendance*. Nothing is harvested
from a page nobody opened, so covering a search meant clicking through it by
hand, one posting at a time.

This queues the visiting. Each URL becomes a `browse_page` task; the extension
opens it in a hidden window, lets it finish, and closes it. The page makes its
own API calls exactly as it would for a person, the interceptor reads them on
the way past, and jobs arrive through the harvest endpoint that already exists.
Nothing here parses anything.

Two plans, and the order matters
--------------------------------
*Searches* find postings that are not in the database at all. A LinkedIn search
page renders cards, so what comes back is title, company, location and an id —
rarely a description, because the page has not been asked for one yet.

*Postings* fill those in. A `/jobs/view/<id>/` page loads the body, which is the
half the search cannot give you. So the useful rhythm is: crawl searches to
discover, then crawl postings to enrich, and the second is where most of the
value is.

Why the pacing is in here rather than left to the client
--------------------------------------------------------
This drives a real browser through a logged-in session. Volume and rhythm are
what anti-automation systems actually measure, and the cost of getting it wrong
is the user's account, not a failed run. So the ceiling on a run and the gap
between pages are settings with conservative defaults, the queue is built to be
drained slowly, and a page already browsed recently is not queued again.
"""

import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus, urlparse

from app.config import settings
from app.services import browser_tasks

logger = logging.getLogger(__name__)

# LinkedIn's job search. `f_TPR=r604800` is "past week": without it a crawl
# spends most of its budget re-reading postings from months ago that are
# already stored, which is the expensive way to harvest nothing.
JOB_SEARCH = (
    "https://www.linkedin.com/jobs/search/?keywords={q}&location={loc}"
    "&f_TPR=r604800&sortBy=DD"
)
JOB_VIEW = "https://www.linkedin.com/jobs/view/{job_id}/"

# A description shorter than this is a card, not a posting — the same bar
# enrichment uses to decide a job still needs its text.
THIN_DESCRIPTION_CHARS = 600

# Sources whose postings live on linkedin.com and can therefore be opened.
_LINKEDIN_SOURCES = ("linkedin", "linkedin_harvest")


def enabled() -> bool:
    return bool(getattr(settings, "BROWSE_ENABLED", True))


def _limit(requested: int | None) -> int:
    ceiling = int(getattr(settings, "BROWSE_MAX_QUEUED", 60))
    return max(1, min(requested or ceiling, ceiling))


def _retry_days() -> int:
    return max(1, int(getattr(settings, "BROWSE_RETRY_DAYS", 30)))


# ---------------------------------------------------------------------------
# What to open
# ---------------------------------------------------------------------------

def search_urls(profile: dict | None) -> list[str]:
    """
    One LinkedIn job search per target role, per location the user cares about.

    Built from the profile rather than typed in, so a crawl covers the same
    ground the fetch cycle does. Roles are crossed with locations because
    LinkedIn scopes a search to one place at a time and "remote" is a location
    like any other to it.
    """
    profile = profile or {}
    roles = [str(r).strip() for r in (profile.get("target_roles") or []) if str(r).strip()]
    if not roles:
        return []

    locations = [
        str(loc).strip()
        for loc in (profile.get("target_locations") or profile.get("locations") or [])
        if str(loc).strip()
    ]
    # Somewhere rather than nowhere: with no stated location a search still
    # runs, and LinkedIn defaults it to the account's own region.
    locations = locations[:4] or [""]

    urls = []
    for role in roles[:6]:
        for location in locations:
            urls.append(
                JOB_SEARCH.format(q=quote_plus(role), loc=quote_plus(location))
            )
    return urls


def posting_urls(db, limit: int | None = None) -> list[str]:
    """
    LinkedIn postings we hold but have no real description for.

    This is the half that pays. A harvested search card carries a title and an
    id and usually no body at all, so these jobs are scored on a fragment — and
    the guest API cannot fix it, which is the whole reason the browser tier
    exists.

    Newest first: a posting from this week is still open, and one from March
    probably is not.
    """
    from sqlalchemy import func, or_

    from app.models.job import Job

    rows = (
        db.query(Job.source_job_id, Job.url)
        .filter(
            Job.source.in_(_LINKEDIN_SOURCES),
            Job.closed_at.is_(None),
            or_(
                Job.description.is_(None),
                func.length(Job.description) < THIN_DESCRIPTION_CHARS,
            ),
        )
        .order_by(Job.fetched_at.desc())
        .limit(_limit(limit) * 3)
        .all()
    )

    urls: list[str] = []
    seen: set[str] = set()
    for source_job_id, url in rows:
        target = _posting_url(source_job_id, url)
        if not target or target in seen:
            continue
        seen.add(target)
        urls.append(target)
    return urls


def _posting_url(source_job_id: str | None, url: str | None) -> str:
    """
    The canonical `/jobs/view/<id>/` URL for one stored job.

    Rebuilt from the id where there is one rather than reusing the stored URL:
    a harvested link often carries tracking parameters, and two of them for the
    same posting would be two tasks opening the same page.
    """
    job_id = str(source_job_id or "").strip()
    if job_id.isdigit():
        return JOB_VIEW.format(job_id=job_id)

    url = str(url or "").strip()
    if not url:
        return ""
    host = (urlparse(url).hostname or "").lower()
    if not (host == "linkedin.com" or host.endswith(".linkedin.com")):
        return ""
    # A stored LinkedIn URL with no id we could parse. Keep it, minus the
    # query string, which is where the tracking lives.
    return url.split("?", 1)[0]


# ---------------------------------------------------------------------------
# Queueing it
# ---------------------------------------------------------------------------

def _already_queued(db, urls: list[str]) -> set[str]:
    """
    URLs with a browse task in flight, or one raised recently.

    Both halves matter. In flight stops a second trigger doubling the queue;
    recently stops a nightly run re-reading the same hundred postings forever
    instead of reaching the ones behind them.
    """
    from app.models.browser_task import BrowserTask

    if not urls:
        return set()

    cutoff = datetime.now(timezone.utc) - timedelta(days=_retry_days())
    rows = (
        db.query(BrowserTask.payload["url"].astext)
        .filter(
            BrowserTask.kind == "browse_page",
            BrowserTask.payload["url"].astext.in_(urls),
            (BrowserTask.status.in_(("queued", "leased")))
            | (BrowserTask.created_at >= cutoff),
        )
        .all()
    )
    return {row[0] for row in rows if row[0]}


def enqueue(db, urls: list[str], limit: int | None = None,
            purpose: str = "harvest") -> int:
    """
    Turn URLs into browse tasks. Returns how many were queued.

    `priority` is left at zero so this never jumps ahead of link resolution or
    an enrichment fetch: those answer a question something is waiting on, and
    this is a background sweep that is worth doing eventually.
    """
    if not enabled() or not urls:
        return 0

    budget = _limit(limit)
    skip = _already_queued(db, urls)
    queued = 0

    for url in urls:
        if queued >= budget:
            break
        if url in skip:
            continue
        browser_tasks.enqueue(
            db, "browse_page",
            {
                "url": url,
                "purpose": purpose,
                # Told per task rather than read from the client's own config,
                # so the pace is one decision made in one place.
                "settle_seconds": int(getattr(settings, "BROWSE_SETTLE_SECONDS", 6)),
                "gap_seconds": int(getattr(settings, "BROWSE_GAP_SECONDS", 20)),
            },
        )
        queued += 1

    if queued:
        logger.info("browse_plan: queued %d page(s) to browse for %s", queued, purpose)
    return queued


def crawl_searches(db, profile: dict | None, limit: int | None = None) -> dict:
    """Queue the searches. Finds postings that are not stored at all."""
    urls = search_urls(profile)
    return {
        "kind": "searches",
        "candidates": len(urls),
        "queued": enqueue(db, urls, limit=limit, purpose="search"),
    }


def crawl_postings(db, limit: int | None = None) -> dict:
    """Queue the postings we hold but have no description for."""
    urls = posting_urls(db, limit=limit)
    return {
        "kind": "postings",
        "candidates": len(urls),
        "queued": enqueue(db, urls, limit=limit, purpose="posting"),
    }


def status(db) -> dict:
    """What the panel shows: how much is waiting, and how long it will take."""
    from app.models.browser_task import BrowserTask

    waiting = (
        db.query(BrowserTask)
        .filter(
            BrowserTask.kind == "browse_page",
            BrowserTask.status.in_(("queued", "leased")),
        )
        .count()
    )
    gap = int(getattr(settings, "BROWSE_GAP_SECONDS", 20))
    settle = int(getattr(settings, "BROWSE_SETTLE_SECONDS", 6))
    return {
        "enabled": enabled(),
        "waiting": waiting,
        "gap_seconds": gap,
        "max_per_run": int(getattr(settings, "BROWSE_MAX_QUEUED", 60)),
        # Stated because "60 pages" means nothing without it, and because the
        # number being large is the feature rather than a problem to fix.
        "eta_minutes": round(waiting * (gap + settle) / 60) if waiting else 0,
    }
