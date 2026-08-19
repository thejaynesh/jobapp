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


class Board:
    """
    One site a crawl can walk.

    `search` is a URL template taking `{q}` and `{loc}`, for boards whose
    search really is expressible as a URL. `entries` are fixed pages to open
    instead — a board's own recommendations or "new this week" list.

    Both exist because boards differ in a way worth being honest about. A
    LinkedIn search URL is public, stable and documented by a decade of use. A
    login-only app like JobRight renders its results from an API and its query
    parameters are internal — guessing at them produces a crawl that opens
    error pages very politely. So those boards get their entry pages, which
    load real listings, and anything more specific is a URL the user pastes in
    from a search they ran themselves.
    """

    def __init__(self, key, host, label, search=None, entries=()):
        self.key = key
        self.host = host
        self.label = label
        self.search = search
        self.entries = tuple(entries)


BOARDS = (
    Board("linkedin", "linkedin.com", "LinkedIn", search=JOB_SEARCH),
    Board(
        "jobright", "jobright.ai", "JobRight",
        # Its recommendations are the board: the whole product is a ranked list
        # per account, so opening it is the equivalent of running a search.
        entries=(
            "https://jobright.ai/jobs/recommend",
            "https://jobright.ai/jobs/search",
        ),
    ),
    Board(
        "hiringcafe", "hiring.cafe", "Hiring Cafe",
        entries=("https://hiring.cafe/",),
    ),
    Board(
        "handshake", "joinhandshake.com", "Handshake",
        entries=("https://app.joinhandshake.com/stu/postings",),
    ),
)

BOARDS_BY_KEY = {board.key: board for board in BOARDS}

# Sources whose postings live on a board a crawl can open. Keyed by host so a
# job's own URL decides, rather than a list of source names that drifts.
_BROWSABLE_HOSTS = tuple(board.host for board in BOARDS)


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

def search_urls(profile: dict | None, boards=None) -> list[str]:
    """
    Where to start looking, per board.

    For a board with a search template that means one search per target role
    per location — built from the profile rather than typed in, so a crawl
    covers the same ground the fetch cycle does, and crossed because these
    sites scope a search to one place at a time ("remote" being a location like
    any other to them).

    For a board without one it means its entry pages, which is not a lesser
    answer: a recommendations feed is that product's search, already filtered
    to the account browsing it.
    """
    profile = profile or {}
    roles = [str(r).strip() for r in (profile.get("target_roles") or []) if str(r).strip()]
    locations = [
        str(loc).strip()
        for loc in (profile.get("target_locations") or profile.get("locations") or [])
        if str(loc).strip()
    ]
    # Somewhere rather than nowhere: with no stated location a search still
    # runs, and the site defaults it to the account's own region.
    locations = locations[:4] or [""]

    urls: list[str] = []
    for board in (boards if boards is not None else BOARDS):
        if board.search:
            if not roles:
                # Nothing to search for. Its entry pages, if it had any, would
                # still be worth opening — but a search board with no query is
                # just the homepage.
                continue
            for role in roles[:6]:
                for location in locations:
                    urls.append(
                        board.search.format(q=quote_plus(role), loc=quote_plus(location))
                    )
        else:
            urls.extend(board.entries)
    return urls


def _host_of(url: str | None) -> str:
    try:
        return (urlparse(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""


def board_for(url: str | None) -> "Board | None":
    """Which board a stored URL belongs to, if any."""
    host = _host_of(url)
    if not host:
        return None
    for board in BOARDS:
        if host == board.host or host.endswith(f".{board.host}"):
            return board
    return None


def posting_urls(db, limit: int | None = None) -> list[str]:
    """
    Postings we hold, on a board we can open, with no real description.

    This is the half that pays. A harvested search card carries a title and an
    id and usually no body at all, so these jobs are scored on a fragment — and
    for a login-only board there is no API that could fix it, which is the
    whole reason the browser tier exists.

    Selected by the job's own URL rather than by a list of source names: a
    posting is browsable if it lives somewhere a browser can reach it, and that
    is a fact about the link, not about which adapter happened to find it.

    Newest first: a posting from this week is still open, and one from March
    probably is not.
    """
    from sqlalchemy import func, or_

    from app.models.job import Job

    host_clause = or_(*[
        Job.url.ilike(f"%{board.host}%") for board in BOARDS
    ])
    rows = (
        db.query(Job.source_job_id, Job.url)
        .filter(
            host_clause,
            Job.closed_at.is_(None),
            or_(
                Job.description.is_(None),
                func.length(Job.description) < THIN_DESCRIPTION_CHARS,
            ),
        )
        .order_by(Job.fetched_at.desc())
        .limit(_limit(limit) * 4)
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
    The URL to open for one stored job.

    LinkedIn's is rebuilt from the posting id where there is one, because a
    harvested LinkedIn link is thick with tracking parameters and two of them
    for the same posting would be two tasks opening one page. Every other board
    keeps its own URL with the query string dropped, which achieves the same
    thing without needing to know how that site builds a link.
    """
    board = board_for(url)
    if board is None:
        return ""

    job_id = str(source_job_id or "").strip()
    if board.key == "linkedin" and job_id.isdigit():
        return JOB_VIEW.format(job_id=job_id)

    url = str(url or "").strip()
    if not url:
        return ""
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


def crawl_searches(db, profile: dict | None, limit: int | None = None,
                   board: str = "") -> dict:
    """
    Queue the searches. Finds postings that are not stored at all.

    `board` narrows it to one site. Worth having because the boards differ in
    how much they cost: LinkedIn is a dozen searches built from the profile,
    JobRight is two pages, and wanting only the second is a normal thing to
    want.
    """
    chosen = [BOARDS_BY_KEY[board]] if board in BOARDS_BY_KEY else None
    urls = search_urls(profile, boards=chosen)
    return {
        "kind": "searches",
        "board": board or "all",
        "candidates": len(urls),
        "queued": enqueue(db, urls, limit=limit, purpose="search"),
    }


def crawl_urls(db, raw: str, limit: int | None = None) -> dict:
    """
    Queue whatever the user pasted in.

    The escape hatch, and not an afterthought. A board's own search is often
    not expressible as a URL anyone else can construct — it is rendered from an
    internal API with parameters that are nobody's business but that app's — so
    guessing at one produces a crawl that opens error pages very politely. A
    search the user ran themselves and copied out of the address bar is exactly
    right, and needs no guessing at all.

    Only http(s), and deduplicated: a pasted list is usually half copy-paste.
    """
    urls: list[str] = []
    seen: set[str] = set()
    for line in str(raw or "").replace(",", "\n").split("\n"):
        candidate = line.strip()
        if not candidate.lower().startswith(("http://", "https://")):
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        urls.append(candidate)

    return {
        "kind": "pasted",
        "candidates": len(urls),
        "queued": enqueue(db, urls, limit=limit, purpose="pasted"),
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
        "boards": [
            {"key": board.key, "label": board.label,
             "searchable": bool(board.search)}
            for board in BOARDS
        ],
        "gap_seconds": gap,
        "max_per_run": int(getattr(settings, "BROWSE_MAX_QUEUED", 60)),
        # Stated because "60 pages" means nothing without it, and because the
        # number being large is the feature rather than a problem to fix.
        "eta_minutes": round(waiting * (gap + settle) / 60) if waiting else 0,
    }
