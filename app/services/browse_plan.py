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
                 feed_setting=None, scroll_passes=None, click_pages=None,
                 alt_hosts=(), submit_search=False):
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
        # Result pages to click through, for a board that paginates in place.
        # A third kind, and the one that was missing: `page_param` covers a
        # board whose page two is a URL, and `scroll_passes` covers one with no
        # page two at all. Hiring Cafe has numbered buttons and one address for
        # all of them, so scrolling reaches the bottom of page one and stops —
        # every visit harvested the first page and nothing else, while
        # reporting a perfectly healthy scroll.
        self.click_pages = click_pages
        # Whether this board needs its search submitting before it will show a
        # full list. A fourth thing that can go wrong, and it looks exactly
        # like a board with nothing to show: the page loads, renders a handful
        # of results, and scrolling reaches the bottom of those few and stops.
        # Pressing Enter in the search box — with nothing typed in it — is what
        # makes the real list appear.
        self.submit_search = bool(submit_search)
        # Other hosts that are this same board. A board whose entry URL
        # redirects lands on a different host, and `board_for` on the final URL
        # then finds nothing — so the board's own depth and pacing stop
        # applying at exactly the moment the page is real.
        self.alt_hosts = tuple(alt_hosts)
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
        # Infinite scroll with lazy loading, so the scroll is the pagination
        # here exactly as it is on Greenhouse's board — and it was silently
        # taking the default 25, the number meant for a board whose depth comes
        # from queueing page two. There is no page two to queue. Twenty-five
        # passes is the first screenful and then a closed tab.
        scroll_passes=150,
    ),
    Board(
        "hiringcafe", "hiring.cafe", "Hiring Cafe",
        entries=("https://hiring.cafe/",),
        # The address redirects: you queue hiring.cafe and the page that loads
        # is hiringcafe.com. Without naming both, everything keyed on the host
        # — the reader's registration, the source name, this board's own depth
        # — applied to the URL we asked for and not the one that rendered.
        alt_hosts=("hiringcafe.com",),
        # Numbered buttons at the bottom, and one URL for all of them. Neither
        # of the other two mechanisms reaches page two: there is no parameter
        # to append, and scrolling stops at the bottom of page one. The click
        # is the only way through.
        click_pages=10,
        # A few screens per page rather than a deep scroll — the depth here
        # comes from the pages, and the scrolling only has to reach the bottom
        # where the controls are and pull in anything that lazy-loads on the
        # way.
        scroll_passes=8,
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
    # Tsenta. An aggregator like JobRight, matching against the profile rather
    # than a keyword, and reading from a much wider set of career pages and
    # boards than either of them — which is the whole reason it is here.
    #
    # Login-only and entirely stateful: the filters live in the app rather than
    # the address, so there is no `{q}` to fill in and no page-two URL to
    # queue. The list is infinite scroll, and it will not show more than a
    # handful of results until its search is submitted — see `submit_search`.
    Board(
        "tsenta", "tsenta.com", "Tsenta",
        feed_setting="BROWSE_TSENTA_FEED",
        # Nothing is typed into the box. The board is already matching against
        # the profile, so submitting it empty is what a person does here, and
        # without it the visit sees five results and calls the list finished.
        submit_search=True,
        # Infinite scroll, so this loop is the pagination. Deep for the same
        # reason Greenhouse is: every batch is another API response the
        # interceptor reads on the way past.
        scroll_passes=200,
    ),
    Board(
        "handshake", "joinhandshake.com", "Handshake",
        # `/job-search` rather than `/stu/postings`: the second is the page the
        # board was added with and the first is where its search actually
        # lives, which its own URL gave away — `?page=1&per_page=25`.
        entries=("https://app.joinhandshake.com/job-search?per_page=25",),
        # An ordinal page number, so the step is 1 and the first page is 1.
        # Sized 25 would ask for `page=26` next, which is the mistake the
        # `page_base`/`page_size` split exists to make hard to write.
        #
        # The entry URL deliberately omits `page=1`: the first page is the URL
        # as given, and carrying the parameter there would put two of them in
        # every later URL.
        page_param="page", page_size=1, page_base=1,
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
_BROWSABLE_HOSTS = tuple(
    host for board in BOARDS for host in (board.host, *board.alt_hosts)
)


def enabled() -> bool:
    return bool(getattr(settings, "BROWSE_ENABLED", True))


def _limit(requested: int | None) -> int:
    ceiling = int(getattr(settings, "BROWSE_MAX_QUEUED", 60))
    return max(1, min(requested or ceiling, ceiling))


def _retry_days() -> int:
    return max(1, int(getattr(settings, "BROWSE_RETRY_DAYS", 30)))


def paused_hosts() -> tuple[str, ...]:
    """
    Hosts nothing may be queued for, from `BROWSE_PAUSED_HOSTS`.

    This exists because of a real warning from LinkedIn: driven browsing opens
    up to `BROWSE_MAX_QUEUED` pages a run through a logged-in session, on a
    schedule, and that is a volume of access no person produces. When a site
    says it has noticed, the answer needed is "stop touching that one, keep the
    rest" — and it is needed in the next minute, not the next deploy.

    Deliberately a host list rather than a board list. A board key only covers
    pages a crawl planned; enrichment queues postings by URL, and the point of
    a pause is that *nothing* goes there.
    """
    raw = str(getattr(settings, "BROWSE_PAUSED_HOSTS", "") or "")
    hosts = []
    for part in raw.replace("\n", ",").split(","):
        host = part.strip().lower().lstrip(".")
        if host:
            hosts.append(host)
    return tuple(hosts)


def is_paused(url: str | None) -> bool:
    """Whether this URL is on a host that has been paused."""
    host = _host_of(url)
    if not host:
        return False
    return any(
        host == paused or host.endswith(f".{paused}")
        for paused in paused_hosts()
    )


def _challenge_hours() -> int:
    return max(1, int(getattr(settings, "BROWSE_CHALLENGE_BACKOFF_HOURS", 24)))


def blocked_hosts(db) -> set[str]:
    """
    Hosts that recently asked for a human check nobody got past.

    A pause you configure is a decision; this is the same shape arrived at by
    observation, and it needs to be automatic because the failure repeats. A
    site that puts a check in front of every page will fail every page — so
    sixty queued visits become sixty raised windows and an evening of the
    browser achieving nothing, which is worse than not trying.

    Deliberately short-lived and re-earned. These checks are usually about the
    traffic pattern rather than the visitor, so a host that blocked us this
    morning is worth one attempt tomorrow — and if it passes, nothing here
    remembers that it ever failed.
    """
    from app.models.agent_event import AgentEvent

    since = datetime.now(timezone.utc) - timedelta(hours=_challenge_hours())
    rows = (
        db.query(AgentEvent.host, AgentEvent.created_at,
                 AgentEvent.summary["challenge"].astext)
        .filter(
            AgentEvent.kind == "browse",
            AgentEvent.created_at >= since,
            AgentEvent.host.isnot(None),
            # `timeout` is nobody home; `skipped` is the extension declining to
            # ask again. Both mean the page was not reached. `passed` is here
            # too, but as the thing that *clears* a block rather than causes
            # one — see below.
            AgentEvent.summary["challenge"].astext.in_(
                ("timeout", "skipped", "passed")),
        )
        .order_by(AgentEvent.created_at.asc())
        .all()
    )

    # Last word per host wins. Passing the check is precisely the event that
    # invalidates an earlier block, and without this the backoff outlived the
    # thing it was waiting for: you would go and pass the check, and the host
    # would stay untouched for the rest of the day anyway — which reads as the
    # click having achieved nothing.
    blocked: set[str] = set()
    for host, _created_at, outcome in rows:
        if not host:
            continue
        if outcome == "passed":
            blocked.discard(host)
        else:
            blocked.add(host)
    return blocked


def _ratelimit_minutes() -> int:
    return max(1, int(getattr(settings, "BROWSE_RATELIMIT_REST_MINUTES", 20)))


def resting_hosts(db) -> set[str]:
    """
    Hosts that asked us to wait, and have not waited long enough yet.

    Different from `blocked_hosts` in the thing that matters — its duration. A
    human check is a door that stays shut until somebody opens it; a rate limit
    is the board saying "not this fast", and it means the few minutes it says.
    Treating the two the same would either hammer a site for a day or abandon
    one for a day, and both are wrong.

    The pages harvested before the limit are already stored, so this is not a
    failure to recover from. It is only the answer to "when is it worth going
    back", and going back is the point: an infinite list cannot be resumed
    part-way, so the next visit re-walks the top of it either way.
    """
    from app.models.agent_event import AgentEvent

    since = datetime.now(timezone.utc) - timedelta(minutes=_ratelimit_minutes())
    rows = (
        db.query(AgentEvent.host)
        .filter(
            AgentEvent.kind == "browse",
            AgentEvent.created_at >= since,
            AgentEvent.host.isnot(None),
            AgentEvent.summary["rate_limited"].astext == "true",
        )
        .distinct()
        .all()
    )
    return {row[0] for row in rows if row[0]}


def tolerated_passes(db, host: str) -> int | None:
    """
    How deep this host let a scroll go before it objected. None if never.

    The number the next visit is built from. Asking for four hundred passes
    from a board that stopped us at forty does not get us to four hundred — it
    gets us to forty and a rate limit, and then a rest we did not need to
    spend. Reading it back and staying under it covers the same ground without
    the penalty, which on a board filtered to the last day is all the ground
    there is.

    The shallowest recent objection wins rather than the average. Being wrong
    low costs a few cards on one visit; being wrong high costs the visit.
    """
    from app.models.agent_event import AgentEvent

    since = datetime.now(timezone.utc) - timedelta(days=7)
    rows = (
        db.query(AgentEvent.summary["passes_done"].astext)
        .filter(
            AgentEvent.kind == "browse",
            AgentEvent.created_at >= since,
            AgentEvent.host == host,
            AgentEvent.summary["rate_limited"].astext == "true",
        )
        .all()
    )
    depths = []
    for row in rows:
        try:
            depth = int(row[0])
        except (TypeError, ValueError):
            continue
        if depth > 0:
            depths.append(depth)
    return min(depths) if depths else None


def is_blocked(host: str, blocked: set[str]) -> bool:
    """Whether a host is covered by `blocked`, subdomains included."""
    if not host:
        return False
    return any(
        host == one or host.endswith(f".{one}") or one.endswith(f".{host}")
        for one in blocked
    )


# ---------------------------------------------------------------------------
# What to open
# ---------------------------------------------------------------------------

def _depth() -> int:
    return max(1, int(getattr(settings, "BROWSE_SEARCH_PAGES", 5)))


def search_urls(profile: dict | None, boards=None, depth: int | None = None,
                db=None) -> list[str]:
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
        # Entry pages get depth too, when a recipe has taught us how. They
        # never had a page parameter written for them, so before this they were
        # one page each however much the board held.
        for entry in entries:
            urls.extend(_pages_for(db, board, entry, pages))

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
                    urls.extend(_pages_for(db, board, first, pages))
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
        for candidate in (board.host, *board.alt_hosts):
            if host == candidate or host.endswith(f".{candidate}"):
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


def _scroll_passes(url: str, db=None) -> int:
    """
    How hard to scroll this URL's page.

    A board that pages by URL wants a few screens — its depth comes from the
    next page being queued. A board that scrolls infinitely has no next page,
    so the scroll *is* the pagination and the number has to be much larger.

    Capped by what the host has actually tolerated, when it has told us. Asking
    a board that stopped us at forty for four hundred does not reach four
    hundred; it reaches forty, a rate limit, and a rest. Backing off a little
    below the objection covers the same ground for free.
    """
    learned = _learned(db, url)
    if learned and learned.get("mode") == "scroll" and learned.get("scroll_passes"):
        asked = max(1, min(400, int(learned["scroll_passes"])))
    else:
        board = board_for(url)
        if board is not None and board.scroll_passes:
            asked = int(board.scroll_passes)
        else:
            asked = max(1, int(getattr(settings, "BROWSE_SCROLL_PASSES", 25)))

    if db is None:
        return asked
    seen = tolerated_passes(db, _host_of(url))
    if not seen:
        return asked
    # A little under, not exactly at: the limit is a rate and the depth it
    # bites at moves around, so sitting on the last known edge would find it
    # again about half the time.
    return max(1, min(asked, int(seen * 0.8)))


def _pages_for(db, board, url: str, depth: int) -> list[str]:
    """
    This URL plus the further result pages it offers.

    A learned `url` recipe wins over the board's own parameter, and gives one
    to a board that never had a parameter written for it — which is most of
    them, since `entries`-only boards were added without any notion of depth.
    """
    learned = _learned(db, url)
    if learned and learned.get("mode") == "url" and depth > 1:
        param = str(learned.get("page_param") or "")
        size = max(1, int(learned.get("page_size") or 25))
        base = int(learned.get("page_base") or 0)
        if param:
            joiner = "&" if "?" in url else "?"
            return [url] + [
                f"{url}{joiner}{param}={base + n * size}"
                for n in range(1, depth)
            ]
    if board is not None:
        return board.pages(url, depth)
    return [url]


def _learned(db, url: str) -> dict | None:
    """
    This host's learned crawl recipe, if it has one.

    Consulted ahead of the hand-written board on purpose. The board entry is a
    guess made once by whoever added the site; the recipe was written against
    evidence from the page as it is now, and withdraws itself when the visits
    it produces stop getting anywhere. When they disagree the newer,
    self-correcting one should win.
    """
    if db is None:
        return None
    try:
        from app.services import crawl_recipes

        # Inside a savepoint. On a deploy where the migration has not run the
        # table is missing, and a failed statement aborts the whole enclosing
        # transaction — so catching the error is not enough: everything the
        # caller does afterwards fails too, with an error naming a query that
        # had nothing to do with it. The savepoint contains the damage to the
        # lookup that caused it.
        with db.begin_nested():
            return crawl_recipes.active_for(db, _host_of(url))
    except Exception as exc:
        logger.debug("browse_plan: no crawl recipe for %s: %s", url, exc)
        return None


def _max_pages(url: str, db=None) -> int:
    """
    Result pages to click through on this URL's board.

    One for everything that does not say otherwise, so a scrolling board is
    untouched by any of this and the extension's behaviour there is exactly
    what it always was.
    """
    learned = _learned(db, url)
    if learned and learned.get("mode") == "click":
        return max(1, min(30, int(learned.get("max_pages") or 10)))
    if learned:
        # A recipe that says this board scrolls, or pages by URL, is also
        # saying it does not click — so it overrides a hand-written
        # `click_pages` rather than being ignored next to it.
        return 1

    board = board_for(url)
    if board is not None and board.click_pages:
        return max(1, int(board.click_pages))
    return 1


def _click_selector(url: str, db=None) -> str:
    """
    The learned next-page control for this board, if one was learned.

    Empty means the extension falls back to its own heuristics — `rel="next"`,
    then a short label reading "Next" or an arrow — which is what every board
    got before any of this and still gets when nothing has been learned.
    """
    learned = _learned(db, url)
    if learned and learned.get("mode") == "click":
        return str(learned.get("selector") or "")[:200]
    return ""


def _submit_search(url: str) -> bool:
    """
    Whether this board hides its list until its search is submitted.

    A board property rather than something the extension guesses at. Pressing
    Enter in a search box is a real interaction with a logged-in session, and
    doing it speculatively on every board would re-run searches that had
    already run — so it happens only where a board is known to need it.
    """
    board = board_for(url)
    return bool(board is not None and board.submit_search)


def _scroll_pause_seconds(url: str, db=None) -> int:
    """
    Seconds to rest between batches on this board.

    Zero by default: pausing on a board that never objected is depth given
    away for nothing. A board that has objected gets a real pause, because the
    limit is a *rate* — going slower reaches further than stopping sooner does,
    and stopping sooner is all a depth cap can buy.
    """
    if db is None:
        return 0
    if not tolerated_passes(db, _host_of(url)):
        return 0
    return max(0, int(getattr(settings, "BROWSE_SCROLL_PAUSE_SECONDS", 2)))


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
    # One query for the batch, like the cooloff above. A request the user is
    # watching ignores it: they are at the keyboard, which is the entire thing
    # the backoff concluded was not true.
    blocked = set() if priority >= PRIORITY_REQUESTED else blocked_hosts(db)
    # A rest is not overridden by a button, and that is the difference between
    # the two. A human check being watched by a human is answerable; a board
    # that said "wait a few minutes" says it just as firmly to somebody sitting
    # at the keyboard, and queueing anyway would spend the request to be told
    # again. The caller reports the wait instead — see `runs.queue_browsing`.
    resting = resting_hosts(db)
    queued = 0

    for url in urls:
        if queued >= budget:
            break
        if url in skip:
            continue
        # Checked here rather than in each planner because this is the one
        # place every browse task is born — a crawl, a re-visit, a pasted URL
        # and an enrichment top-up all arrive through it. A pause enforced
        # anywhere else is a pause with a way around it.
        if is_paused(url):
            continue
        if blocked and is_blocked(_host_of(url), blocked):
            continue
        if resting and is_blocked(_host_of(url), resting):
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
                "scroll_passes": _scroll_passes(url, db),
                "scroll_pause_seconds": _scroll_pause_seconds(url, db),
                "max_pages": _max_pages(url, db),
                # Empty unless a recipe named one. The extension keeps its own
                # heuristics for everything else.
                "click_selector": _click_selector(url, db),
                # Whether to press Enter in this board's search box before
                # scrolling. False everywhere but the boards that need it: on
                # any other site this would re-run a search the page had
                # already run, which at best costs a round trip and at worst
                # scrolls back to the top.
                "submit_search": _submit_search(url),
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
    urls = search_urls(profile, boards=chosen, db=db)
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

    # Before counting the queue, not after: pages for a host paused since they
    # were queued are not work, and leaving them in the count would hold the
    # top-up off on the strength of a backlog that must never run.
    drop_paused(db)

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


def drop_paused(db) -> int:
    """
    Throw away queued pages for hosts that are now paused. Returns how many.

    Pausing a host stops new work being queued, but the queue is a plan made
    earlier — and after a site has warned you, sixty of its pages already
    waiting is the whole problem, not a detail. Runs on startup and whenever
    the pause list is read on the panel, so setting the variable is the only
    step.

    Leased rows are left alone for the same reason `drop_queued` leaves them:
    something is mid-visit and deleting the row would not close the window.
    """
    from app.models.browser_task import BrowserTask

    hosts = paused_hosts()
    if not hosts:
        return 0

    rows = (
        db.query(BrowserTask)
        .filter(BrowserTask.kind == "browse_page",
                BrowserTask.status == "queued")
        .all()
    )
    dropped = 0
    for row in rows:
        if is_paused((row.payload or {}).get("url")):
            db.delete(row)
            dropped += 1
    if dropped:
        db.commit()
        logger.info(
            "browse_plan: dropped %d queued page(s) for paused host(s) %s",
            dropped, ", ".join(hosts),
        )
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
            "batches": (row.summary or {}).get("batches") or 0,
            "scroll_target": (row.summary or {}).get("scroll_target") or "",
            # None on every board but the ones that hide their list until the
            # search is run. False there means the box was gone, and the
            # shallow visit that follows is that rather than an empty board.
            "searched_ok": (row.summary or {}).get("searched_ok"),
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
    paused = paused_hosts()
    return {
        "enabled": enabled(),
        "waiting": waiting,
        "paused": list(paused),
        # Named separately from `paused` because the remedy differs: a pause is
        # yours to lift, whereas a host here is asking you to go and click
        # something, and will keep asking until you do.
        "blocked": sorted(blocked_hosts(db)),
        # And separately again from both: this one needs nothing from you but
        # time, so a panel that told you to go and fix something would be
        # asking for work that is not there.
        "resting": sorted(resting_hosts(db)),
        "rest_minutes": _ratelimit_minutes(),
        "boards": [
            {"key": board.key, "label": board.label,
             "searchable": bool(board.search),
             # Shown struck through rather than hidden. A board that vanished
             # from the panel reads as a bug; one labelled paused reads as a
             # decision, which it was.
             "paused": is_paused(f"https://{board.host}/")}
            for board in BOARDS
        ],
        "gap_seconds": gap,
        "max_per_run": int(getattr(settings, "BROWSE_MAX_QUEUED", 60)),
        # Stated because "60 pages" means nothing without it, and because the
        # number being large is the feature rather than a problem to fix.
        "eta_minutes": round(waiting * (gap + settle) / 60) if waiting else 0,
    }
