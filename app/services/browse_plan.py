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

    `page_param` and `page_size` say how the board paginates. Without them a
    search is one page — which for LinkedIn is twenty-five cards, and is why
    the crawl looked like it was not discovering anything. Depth is what turns
    a search into a sweep, and it costs nothing but more queued pages.

    `feed_setting` names a setting holding the URLs instead, for a board whose
    location filter is not composable — Greenhouse carries a place name, a
    latitude, a longitude and a country code that all have to agree, so
    substituting a location from the profile would produce coordinates in
    Kansas labelled London. Those URLs are copied from the address bar with
    `{q}` marking the keyword, and read at call time so changing them is a
    setting rather than a deploy.
    """

    def __init__(self, key, host, label, search=None, entries=(),
                 page_param=None, page_size=25, page_base=0,
                 feed_setting=None, scroll_passes=None):
        self.key = key
        self.host = host
        self.label = label
        self.search = search
        self.entries = tuple(entries)
        self.feed_setting = feed_setting
        # How hard to scroll one of this board's pages. For a board that pages
        # by URL a few screens is plenty — the depth comes from queueing the
        # next page. For one that scrolls infinitely there *is* no next page,
        # so this loop is the pagination and the number has to be much larger.
        self.scroll_passes = scroll_passes
        self.page_param = page_param
        self.page_size = max(1, page_size)
        # What the parameter reads on the first page. Boards count two
        # different ways — an offset in results (`start=0, 25, 50`) or an
        # ordinal page (`page=1, 2, 3`) — and assuming the first turns the
        # second page of an ordinal board back into the first, so every search
        # would fetch page one twice and never reach page four.
        self.page_base = page_base

    def resolve(self) -> tuple[str | None, tuple[str, ...]]:
        """
        This board's `(search, entries)`, reading its setting where it has one.

        A configured URL containing `{q}` is a search template; one without is
        a page to open as-is, for a filter set that needs no keyword. Splitting
        on that rather than on a second setting keeps "paste what is in your
        address bar" as the whole instruction.
        """
        if not self.feed_setting:
            return self.search, self.entries

        raw = str(getattr(settings, self.feed_setting, "") or "")
        urls = [part.strip() for part in raw.split(",") if part.strip()]
        searches = [url for url in urls if "{q}" in url]
        fixed = tuple(url for url in urls if "{q}" not in url)
        return (searches[0] if searches else None), fixed

    def pages(self, url: str, depth: int) -> list[str]:
        """
        `url` plus however many further result pages this board offers.

        The first page is the URL as given, so a board with no pagination
        scheme is not a special case anywhere else.
        """
        if not self.page_param or depth <= 1:
            return [url]
        joiner = "&" if "?" in url else "?"
        return [url] + [
            f"{url}{joiner}{self.page_param}={self.page_base + n * self.page_size}"
            for n in range(1, depth)
        ]


BOARDS = (
    # 25 per page is what LinkedIn's own paging uses; `start` is its parameter.
    Board("linkedin", "linkedin.com", "LinkedIn", search=JOB_SEARCH,
          page_param="start", page_size=25),
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
    # Greenhouse's own job-seeker board: every posting on the platform rather
    # than one company's. Worth crawling for the postings, but worth far more
    # for the slugs — the Greenhouse source adapter is entirely slug-driven,
    # and one slug returns that company's whole board with full descriptions
    # through a free API, forever. See `harvest._mine_ats_boards`.
    #
    # Login-only, so the server cannot reach it at all; the browser can,
    # because it is already signed in.
    #
    # An entry URL rather than a search template because this board filters
    # rather than searches — the URL carries location, date and salary but no
    # keyword — so there is no `{q}` to fill in. It is `BROWSE_GREENHOUSE_FEED`
    # so the filters can be changed without a deploy: paste a new one from the
    # address bar after setting them how you want.
    Board(
        "greenhouse", "my.greenhouse.io", "Greenhouse (all companies)",
        feed_setting="BROWSE_GREENHOUSE_FEED",
        # Infinite scroll, no page-two URL to queue. Every batch this pulls in
        # is another API response the interceptor reads, and on this board each
        # one carries company slugs — so scrolling deep here buys permanent
        # sources rather than just more rows.
        scroll_passes=200,
    ),
    Board(
        "handshake", "joinhandshake.com", "Handshake",
        entries=("https://app.joinhandshake.com/stu/postings",),
    ),
    # Company careers sites. Worth crawling for the same reason LinkedIn is —
    # they are their own board with no public API — and worth nothing at all
    # for a company on Greenhouse, Lever or Ashby, because there is already a
    # source adapter reading that company's API directly and faster.
    #
    # These two search URLs are public and stable in the sense that they are
    # what the site's own search box produces, but neither is documented, and
    # they are a step less certain than LinkedIn's. If one stops returning
    # anything the Harvest by site panel reports it as "Forwarding, never finds
    # jobs" rather than failing silently.
    Board(
        "amazon", "amazon.jobs", "Amazon Jobs",
        search="https://www.amazon.jobs/en/search?base_query={q}&loc_query={loc}",
        page_param="offset", page_size=10,
    ),
    Board(
        "google", "google.com", "Google Careers",
        search=(
            "https://www.google.com/about/careers/applications/jobs/results"
            "?q={q}&location={loc}"
        ),
        # Ordinal pages rather than an offset: page 1 is the first, so the
        # second is 2.
        page_param="page", page_size=1, page_base=1,
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

def _depth() -> int:
    return max(1, int(getattr(settings, "BROWSE_SEARCH_PAGES", 5)))


def search_urls(profile: dict | None, boards=None, depth: int | None = None) -> list[str]:
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

    pages = _depth() if depth is None else max(1, depth)
    urls: list[str] = []
    for board in (boards if boards is not None else BOARDS):
        search, entries = board.resolve()
        # Its fixed pages either way: a board can have both a keyword search
        # and a filter set that needs no keyword, and the second is not a
        # fallback for the first.
        urls.extend(entries)

        if search:
            if not roles:
                # Nothing to search for. Its entry pages are already queued
                # above; a search board with no query is just the homepage.
                continue
            for role in roles[:6]:
                for location in locations:
                    first = search.format(
                        q=quote_plus(role), loc=quote_plus(location),
                    )
                    # Every result page, not just the first. One page is
                    # twenty-five cards, which is what made a "crawl" look like
                    # it was finding nothing.
                    urls.extend(board.pages(first, pages))
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

# Where a browse task sits in the queue. One scheme in one place, because the
# numbers only mean anything relative to each other — and they were not: a
# crawl the user asked for went in at 0 while enrichment's own browser work
# went in at 2, so an explicit request queued behind up to five hundred
# background pages at twenty seconds each. Pressing a button and seeing
# nothing happen for three hours is indistinguishable from a broken feature.
PRIORITY_REQUESTED = 9   # a person pressed a button and is watching
PRIORITY_ENRICHMENT = 2  # a job is waiting on this description (set elsewhere)
PRIORITY_SWEEP = 0       # the scheduled top-up, worth doing eventually


def _scroll_passes(url: str) -> int:
    """
    How hard to scroll this URL's page.

    A board that pages by URL wants a few screens — its depth comes from the
    next page being queued. A board that scrolls infinitely has no next page,
    so the scroll *is* the pagination and the number has to be much larger.
    """
    board = board_for(url)
    if board is not None and board.scroll_passes:
        return int(board.scroll_passes)
    return max(1, int(getattr(settings, "BROWSE_SCROLL_PASSES", 25)))


def _already_queued(db, urls: list[str],
                    respect_cooloff: bool = True) -> set[str]:
    """
    URLs not worth queueing again right now.

    Two different reasons to skip one, and they do not apply to the same
    callers.

    *In flight* — queued or leased — always skips. A second trigger should not
    double the queue, and nothing is gained by opening one page twice at once.

    *Visited recently* skips only an unattended run. The cooloff exists so a
    nightly sweep does not re-read the same hundred pages forever instead of
    reaching the ones behind them. Applied to a button press it is simply
    wrong: pressing "crawl this board" an hour after the last crawl means *do
    it again*, and a thirty-day cooloff turned that into a button that queued
    nothing, said nothing, and looked broken.
    """
    from app.models.browser_task import BrowserTask

    if not urls:
        return set()

    in_flight = BrowserTask.status.in_(("queued", "leased"))
    if respect_cooloff:
        cutoff = datetime.now(timezone.utc) - timedelta(days=_retry_days())
        recency = in_flight | (BrowserTask.created_at >= cutoff)
    else:
        recency = in_flight

    rows = (
        db.query(BrowserTask.payload["url"].astext)
        .filter(
            BrowserTask.kind == "browse_page",
            BrowserTask.payload["url"].astext.in_(urls),
            recency,
        )
        .all()
    )
    return {row[0] for row in rows if row[0]}


def enqueue(db, urls: list[str], limit: int | None = None,
            purpose: str = "harvest",
            priority: int = PRIORITY_SWEEP) -> int:
    """
    Turn browse URLs into tasks. Returns how many were queued.

    `priority` is the caller saying whether anyone is waiting. A scheduled
    sweep is worth doing eventually and belongs behind everything; a crawl
    somebody just asked for belongs in front, because the queue is never empty
    and "eventually" in a full queue is hours.
    """
    if not enabled() or not urls:
        return 0

    budget = _limit(limit)
    # A request the user is watching ignores the cooloff; the scheduled sweep
    # keeps it. Derived from priority rather than passed separately, because
    # "somebody is waiting on this" and "they want it now" are the same fact.
    skip = _already_queued(
        db, urls, respect_cooloff=priority < PRIORITY_REQUESTED,
    )
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
                # Read back off the URL rather than passed down from the
                # caller: `enqueue` takes a flat list, and a board that needs
                # two hundred scrolls should get them whether its URLs came
                # from a crawl, a re-visit, or the paste box.
                "scroll_passes": _scroll_passes(url),
            },
            priority=priority,
        )
        queued += 1

    if queued:
        logger.info("browse_plan: queued %d page(s) to browse for %s", queued, purpose)
    return queued


def crawl_searches(db, profile: dict | None, limit: int | None = None,
                   board: str = "", priority: int = PRIORITY_REQUESTED) -> dict:
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
        "queued": enqueue(db, urls, limit=limit, purpose="search",
                          priority=priority),
    }


def crawl_urls(db, raw: str, limit: int | None = None,
               priority: int = PRIORITY_REQUESTED) -> dict:
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
        "queued": enqueue(db, urls, limit=limit, purpose="pasted",
                          priority=priority),
    }


def crawl_postings(db, limit: int | None = None,
                   priority: int = PRIORITY_REQUESTED) -> dict:
    """Queue the postings we hold but have no description for."""
    urls = posting_urls(db, limit=limit)
    return {
        "kind": "postings",
        "candidates": len(urls),
        "queued": enqueue(db, urls, limit=limit, purpose="posting",
                          priority=priority),
    }


def agent_seen_recently(db, hours: int | None = None) -> bool:
    """
    Whether a browser has asked for work lately.

    The one precondition worth checking before a *scheduled* crawl, and not
    before a triggered one. A button press is a person saying "do this now",
    and their laptop is by definition awake. A timer firing at four in the
    morning has no such evidence, and queueing sixty pages for an agent that
    has not polled since Tuesday just fills the queue with work that expires
    unread — which then hides the real backlog behind it.
    """
    from app.models.browser_task import BrowserTask
    from app.services import browser_tasks

    window = timedelta(hours=hours if hours is not None
                       else int(getattr(settings, "BROWSE_AGENT_STALE_HOURS", 24)))
    cutoff = datetime.now(timezone.utc) - window

    seen = browser_tasks.last_agent(db)
    if seen and seen.get("at"):
        try:
            polled = datetime.fromisoformat(str(seen["at"]).replace("Z", "+00:00"))
            if polled.tzinfo is None:
                polled = polled.replace(tzinfo=timezone.utc)
            if polled >= cutoff:
                return True
        except (TypeError, ValueError):
            pass

    # A lease is proof too, and survives a profile blob that was never written.
    return bool(
        db.query(BrowserTask)
        .filter(BrowserTask.leased_at.isnot(None), BrowserTask.leased_at >= cutoff)
        .first()
    )


def scheduled_crawl(db, profile: dict | None) -> dict:
    """
    The crawl on a timer rather than a button.

    Three guards, each answering a way this could quietly go wrong:

    *Nobody home.* Queueing for an agent that has not polled in a day fills the
    queue with tasks that expire unread and buries whatever is behind them.

    *Still working.* The queue drains at a human pace — a page every twenty
    seconds — so a run queued every few hours would outrun the browser by an
    order of magnitude. Topping up only when the queue is nearly empty keeps
    the schedule honest about being a backstop rather than a firehose.

    *Descriptions before discovery.* A posting already stored with no
    description is worth more than a posting nobody has seen: it has been
    scored on a fragment, and the fragment is why. So the backlog is served
    first and searching only happens once it is drained.
    """
    if not enabled():
        return {"queued": 0, "skipped": "disabled"}

    if not agent_seen_recently(db):
        logger.info("browse_plan: no agent has polled lately; queueing nothing")
        return {"queued": 0, "skipped": "no agent"}

    waiting = status(db)["waiting"]
    floor = max(0, int(getattr(settings, "BROWSE_TOPUP_BELOW", 10)))
    if waiting > floor:
        return {"queued": 0, "skipped": "queue still draining", "waiting": waiting}

    # Behind everything, including a crawl the user asked for an hour ago. A
    # timer has nobody waiting on it.
    outcome = crawl_postings(db, priority=PRIORITY_SWEEP)
    if outcome["queued"]:
        return {**outcome, "skipped": None}

    return {**crawl_searches(db, profile, priority=PRIORITY_SWEEP),
            "skipped": None}


def drop_queued(db, purpose: str = "") -> int:
    """
    Throw away browse work that has not started. Returns how many went.

    Worth having because the queue is a plan, not a promise. Sixty postings
    queued this morning are sixty pages of a backlog that may no longer be what
    you want the browser spending its evening on, and until now the only way to
    change your mind was to wait it out — at twenty seconds a page.

    Leased tasks are left alone: something is mid-visit, and cancelling the row
    would not close the window.
    """
    from app.models.browser_task import BrowserTask

    query = db.query(BrowserTask).filter(
        BrowserTask.kind == "browse_page", BrowserTask.status == "queued",
    )
    if purpose:
        query = query.filter(BrowserTask.payload["purpose"].astext == purpose)

    dropped = query.delete(synchronize_session=False)
    db.commit()
    if dropped:
        logger.info("browse_plan: dropped %d queued page(s)", dropped)
    return dropped


def recent_visits(db, limit: int = 12) -> list[dict]:
    """
    Pages the browser has actually opened, newest first.

    The gap this closes: a crawl was queued, drained and finished with nothing
    to show for it but a number on a panel. "Did my Greenhouse crawl run?" had
    no answer — not because the information was missing, but because nothing
    displayed it.
    """
    from app.models.agent_event import AgentEvent

    rows = (
        db.query(AgentEvent)
        .filter(AgentEvent.kind == "browse")
        .order_by(AgentEvent.created_at.desc())
        .limit(max(1, limit))
        .all()
    )
    return [
        {
            "host": row.host or "unknown",
            "at": row.created_at,
            "ok": bool(row.ok),
            "purpose": (row.summary or {}).get("purpose") or "",
            "title": (row.summary or {}).get("title") or "",
            # The measure of whether a visit went deep or gave up on the first
            # stall — the only thing that says an infinite-scroll board was
            # actually walked.
            "scrolled_px": (row.summary or {}).get("scrolled_px") or 0,
        }
        for row in rows
    ]


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
