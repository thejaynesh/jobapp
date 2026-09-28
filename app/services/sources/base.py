import contextvars
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable
from app.config import live

logger = logging.getLogger(__name__)

# Board fetches are network-bound and independent per company, so they run in a
# small thread pool. This is what makes carrying hundreds of discovered company
# slugs per cycle affordable instead of a serial multi-minute crawl.
DEFAULT_BOARD_WORKERS = 8


class SourceUnavailable(Exception):
    """
    The source has told us to stop — bad credentials, quota spent, or a rate
    limit. Retrying the remaining query/location combinations can only produce
    the same answer, so the caller abandons this source for the cycle instead of
    generating dozens of identical errors.
    """


# Statuses that mean "stop asking", as opposed to a transient server fault.
BLOCKING_STATUSES = frozenset({401, 402, 403, 429})


def is_bot_challenge(resp) -> bool:
    """
    Whether this is an anti-bot interstitial rather than the site's answer.

    Worth telling apart because the fix is completely different. A 403 from a
    key is fixed in the provider's dashboard; a Cloudflare "Just a moment…"
    page is a verdict on the server's IP, which no key, header or retry
    changes — the browser extension, on a residential connection, is the
    route that works.
    """
    try:
        if resp.headers.get("cf-mitigated") == "challenge":
            return True
        if resp.status_code not in (403, 429, 503):
            return False
        head = (resp.text or "")[:4000].lower()
    except Exception:
        return False
    return any(marker in head for marker in (
        "just a moment", "cf-challenge", "challenge-platform",
        "attention required", "verify you are human",
    ))


def raise_if_blocked(resp, source: str) -> None:
    """Turn an auth/quota/rate-limit response into SourceUnavailable."""
    if is_bot_challenge(resp):
        raise SourceUnavailable(
            f"{source} answered with a bot challenge (HTTP {resp.status_code}) — "
            f"it refuses server IPs outright; the browser extension collects "
            f"it instead"
        )
    if resp.status_code in BLOCKING_STATUSES:
        raise SourceUnavailable(
            f"{source} returned HTTP {resp.status_code}; skipping the rest of "
            f"this source for this cycle"
        )


# What each board listed this cycle, for closing the postings it no longer
# does (`job_fetcher._close_vanished`). A board adapter that reads a company's
# whole listing in one response calls `saw_postings` inside its per-board
# fetch; `fetch_boards_concurrently` files the report under (source, slug) —
# but only for a board whose fetch returned without raising, since a board
# that failed listed nothing and says nothing about what has closed.
_SIGHTINGS: ContextVar = ContextVar("board_sightings", default=None)
_sighting = threading.local()


@contextmanager
def collect_board_sightings():
    """Collect `{(source, slug): {source_job_id, ...}}` for the block's board reads."""
    store: dict[tuple[str, str], set[str]] = {}
    token = _SIGHTINGS.set(store)
    try:
        yield store
    finally:
        _SIGHTINGS.reset(token)


# Postings already stored with their text, per source, for adapters that can
# list a board without the text and fetch it only for what is new
# (`sources.greenhouse`). Loaded by the fetcher once per cycle; read by the
# adapter in the calling thread, since it does not reach the board workers.
_DESCRIBED: ContextVar = ContextVar("described_postings", default=None)


@contextmanager
def known_descriptions(by_source: dict[str, set[str]] | None):
    token = _DESCRIBED.set(by_source or {})
    try:
        yield
    finally:
        _DESCRIBED.reset(token)


def described(source: str) -> frozenset[str]:
    """The `source_job_id`s of `source` already stored with a description."""
    return frozenset((_DESCRIBED.get() or {}).get(source) or ())


def saw_postings(ids) -> None:
    """
    Every posting the board being fetched lists, as the `source_job_id` the
    adapter stores — including the ones it goes on to drop for age, since a
    posting too old to keep is still open.
    """
    _sighting.ids = {str(i) for i in ids if i not in (None, "")}


def fetch_boards_concurrently(
    slugs: list[str],
    fetch_one: Callable[[str], list[dict]],
    label: str,
    workers: int = DEFAULT_BOARD_WORKERS,
) -> list[dict]:
    """
    Run `fetch_one(slug)` for every slug across a thread pool and return the
    flattened jobs, each tagged with the `ats_slug` it came from so the caller
    can attribute per-board yield. A slug that raises is logged and skipped —
    one dead board never costs the rest of the cycle.
    """
    if not slugs:
        return []

    # Log under the ATS's own logger so per-slug failures are attributed to that
    # source rather than to this shared helper (see services.source_diagnostics).
    board_logger = logging.getLogger(f"{__package__}.{label.lower()}")
    sightings = _SIGHTINGS.get()
    lock = threading.Lock()

    def _guarded(slug: str) -> list[dict]:
        _sighting.ids = None
        try:
            jobs = fetch_one(slug) or []
        except Exception as exc:
            board_logger.error("%s fetch error for slug '%s': %s", label, slug, exc)
            return []
        listed = getattr(_sighting, "ids", None)
        if sightings is not None and listed is not None:
            with lock:
                sightings[(label.lower(), slug)] = listed
        for job in jobs:
            job.setdefault("ats_slug", slug)
        return jobs

    # Each board runs in a copy of this thread's context, so the cycle's
    # settings (`cycle_settings`) reach it: a bare pool thread sees none, and
    # every setting read there would go back to the profile.
    parent = contextvars.copy_context()
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(slugs)))) as pool:
        results = list(pool.map(lambda slug: parent.copy().run(_guarded, slug), slugs))

    jobs = [job for board_jobs in results for job in board_jobs]
    board_logger.info("%s: %d jobs across %d companies", label, len(jobs), len(slugs))
    return jobs


def age_cutoff(max_age_days=None):
    """
    The oldest posting date a board adapter should keep, or None for no limit.

    `max_age_days` is the value the fetcher resolved from the settings page;
    None means the caller did not pass one (the environment then decides). Zero
    means no limit, as it does in the fetcher. These adapters used to read the
    environment directly and turn 0 into 30 — so the "Maximum job age" setting
    changed the fetcher's filter and silently not theirs.
    """
    from datetime import datetime, timedelta, timezone

    if max_age_days is None:
        max_age_days = getattr(live(), "MAX_JOB_AGE_DAYS", 30)
    try:
        days = float(max_age_days)
    except (TypeError, ValueError):
        return None
    if days <= 0:
        return None
    return datetime.now(timezone.utc) - timedelta(days=days)


# "2 Days Ago", "Reposted Yesterday", "18 hours ago", "30+ days ago", "a day
# ago". Built In's cards and Google's job results both state age this way
# rather than as a date.
_RELATIVE_AGE = re.compile(
    r"(?:(?P<n>\d+|an?)\+?\s+(?P<unit>minute|hour|day|week|month)s?\s+ago)"
    r"|(?P<yesterday>yesterday)|(?P<today>today|just now|just posted)",
    re.I,
)
_AGE_UNIT_DAYS = {"minute": 1 / 1440, "hour": 1 / 24, "day": 1, "week": 7, "month": 30}


def posted_at_from_age(text: str | None, now=None) -> str | None:
    """
    An ISO timestamp from a relative age, or None when there is none to read.

    Approximate by nature — "2 months ago" is taken as sixty days — which is
    fine for what reads it: the age filter, which needs to know whether a
    posting is days or months old. "30+ days ago" is read as thirty, the
    youngest it could be, so the filter errs toward keeping it.
    """
    from datetime import datetime, timedelta, timezone

    match = _RELATIVE_AGE.search(text or "")
    if not match:
        return None
    if match.group("yesterday"):
        days = 1.0
    elif match.group("today"):
        days = 0.0
    else:
        count = match.group("n").lower()
        number = 1 if count in ("a", "an") else int(count)
        days = number * _AGE_UNIT_DAYS[match.group("unit").lower()]
    now = now or datetime.now(timezone.utc)
    return (now - timedelta(days=days)).isoformat()


def rank_by_title(items: list, queries: list[str], title_of) -> list:
    """
    `items` with the titles matching wants first, for spending a detail budget.

    Three tiers, the way enrichment ranks its own targets
    (`enrichment.select_targets`): a match on a specific word, then anything
    the matcher's filter would pass, then the rest. Stable within a tier.
    Nothing is dropped. Falls back to the given order if the matcher cannot be
    consulted.
    """
    if not queries:
        return list(items)
    try:
        from app.services.matcher import _title_matches_roles, title_priority_match
    except Exception as exc:  # pragma: no cover — an import cycle would be a bug
        logger.warning("title ranking unavailable (%s); keeping order", exc)
        return list(items)

    def tier(item) -> int:
        title = title_of(item) or ""
        if title_priority_match(title, queries):
            return 0
        return 1 if _title_matches_roles(title, queries) else 2

    return sorted(items, key=tier)


def passing_titles(items: list, queries: list[str], title_of) -> list:
    """
    The items whose title the matcher's filter would accept.

    For the big-employer feeds that return a company's *every* opening — two
    thousand at L3Harris, most of them in finance and HR — where storing the
    rest only for the matcher to file them as `title_mismatch` is pure
    ballast. Keyword-searched sources get the same effect from the search.
    Falls open: no queries, or a matcher that cannot be consulted, keeps all.
    """
    if not queries:
        return list(items)
    try:
        from app.services.matcher import _title_matches_roles
    except Exception as exc:  # pragma: no cover
        logger.warning("title gate unavailable (%s); keeping all", exc)
        return list(items)
    return [item for item in items if _title_matches_roles(title_of(item) or "", queries)]


# Labels that are part of a careers host rather than the employer's name.
_HOST_NOISE = frozenset({
    "www", "careers", "career", "jobs", "job", "apply", "hiring", "work",
    "join", "talent", "recruiting", "eightfold", "ai", "com", "net", "org",
    "io", "co", "us",
})


def company_from_host(host: str) -> str:
    """
    A readable employer name out of a careers host, as a last resort.

    `qualcomm.eightfold.ai` → "Qualcomm", `apply.careers.microsoft.com` →
    "Microsoft", `careers.mastercard.com` → "Mastercard". Only used when the
    board registry has no name for the board: a board filed under a real name
    keeps it (see `job_fetcher._name_board_jobs`).
    """
    labels = [p for p in (host or "").lower().split(".") if p]
    for label in labels:
        if label not in _HOST_NOISE and not label.startswith("wd"):
            return label.replace("-", " ").title()
    return host


_US_STATES = frozenset({
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI",
    "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN",
    "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH",
    "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA",
    "WV", "WI", "WY",
})


def in_united_states(location: str | None) -> bool:
    """
    Whether a location string plainly names a place in the US.

    For deciding what currency an unlabelled pay band is in, so it only says
    yes when the string does: "Austin, TX", "New York, NY 10001", "Remote,
    United States". "Anywhere" and "2 Locations" are no, not a guess.
    """
    text = (location or "").strip()
    if re.search(r"\b(?:united states|usa)\b", text, re.I):
        return True
    match = re.search(r",\s*([A-Z]{2})\b(?:\s+\d{5})?\s*$", text)
    return bool(match and match.group(1) in _US_STATES)


# The settings a fetch cycle is running under — `settings` with the settings
# page's overrides on top — for the helpers every adapter calls itself.
#
# `_run_all_adapters` hands each adapter the overlay as `cfg` where it reads a
# value directly, but a dozen board adapters ask `board_workers()` on their own,
# and threading `cfg` through all twelve signatures for one number is how a
# setting ends up wired into eleven of them. Set once per cycle; read here.
_CYCLE_CFG: ContextVar = ContextVar("source_cycle_cfg", default=None)


@contextmanager
def cycle_settings(cfg):
    """
    Make `cfg` what `board_workers()` and friends read, for this block — and
    what `tunables.live()` returns, so code anywhere inside the cycle reads
    the overlay it started with rather than the profile again.
    """
    from app.services import tunables

    token = _CYCLE_CFG.set(cfg)
    try:
        with tunables.bound(cfg):
            yield cfg
    finally:
        _CYCLE_CFG.reset(token)


def cycle_cfg():
    """
    The running cycle's settings overlay; outside one, the settings page's
    values as they are now (`tunables.live()`).
    """
    cfg = _CYCLE_CFG.get()
    if cfg is not None:
        return cfg
    from app.services import tunables
    return tunables.live()


def board_workers() -> int:
    try:
        return max(1, int(getattr(cycle_cfg(), "ATS_BOARD_FETCH_WORKERS",
                                  DEFAULT_BOARD_WORKERS)))
    except (TypeError, ValueError):
        return DEFAULT_BOARD_WORKERS


# Browser-ish headers. Several ATS careers pages answer a bare httpx request
# with a redirect to a consent page and a full one with the listing.
LISTING_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def jobs_from_listing(
    url: str,
    source: str,
    slug: str,
    company: str = "",
    timeout: int = 15,
) -> list[dict]:
    """
    Read JSON-LD, then supported public listing-card or embedded-data formats.

    A listing may omit JSON-LD even when individual detail pages publish it.

    Listing pages routinely omit the description from those blocks. That used
    to make this approach useless; it doesn't now, because enrichment fetches
    the full posting from the URL each block carries.
    """
    import httpx

    from app.services.enrichment import json_ld_postings

    resp = httpx.get(url, headers=LISTING_HEADERS, timeout=timeout,
                     follow_redirects=True)
    resp.raise_for_status()

    postings = json_ld_postings(resp.text)
    if not postings:
        from app.services.sources.listing_fallbacks import extract_listing_jobs

        found = extract_listing_jobs(
            resp.text, str(resp.url) if isinstance(resp.url, httpx.URL) else url,
            source, slug,
        )
        if found:
            return found
        # Distinguish "this board has no openings" from "we cannot read this
        # board any more" — they look identical from the job count alone, and
        # the second one is the failure that goes unnoticed for months.
        board_logger = logging.getLogger(f"{__package__}.{source}")
        if len(resp.text) > 2000:
            board_logger.warning(
                "%s/%s: %d bytes returned but no JobPosting structured data "
                "found (the board's markup may have changed)",
                source, slug, len(resp.text),
            )
        return []

    jobs = []
    for posting in postings:
        title = posting["title"]
        description = posting["description"]
        location = posting["location"]
        jobs.append({
            "source": source,
            # The board's own id where it published one; the URL heuristic only
            # as a fallback. Both can be None, which layer 2 handles.
            "source_job_id": (
                posting.get("identifier") or _listing_job_id(posting["url"])
            ),
            "title": title,
            "company": company or posting["company"] or slug,
            "location": location,
            "is_remote": "remote" in f"{location} {title}".lower(),
            "url": posting["url"],
            "description": description,
            "experience_level": parse_experience_level(title, description),
            "posted_at": posting["posted_at"],
            # Passed through rather than dropped: the block already stated it,
            # and re-deriving it from prose later costs a model call.
            "salary_min": posting["salary_min"],
            "salary_max": posting["salary_max"],
            "salary_currency": posting["salary_currency"],
            # schema.org's `unitText`, which the block states outright. Without
            # it the band cannot be annualised and the salary floor cannot see
            # it.
            "salary_period": posting.get("salary_period"),
            "employment_type": _employment_type(posting["employment_type"]),
        })
    return jobs


# schema.org spells these FULL_TIME / PART_TIME / CONTRACTOR / INTERN; the
# `jobs` column uses the vocabulary job_details normalises to.
_EMPLOYMENT_TYPES = {
    "full_time": "full_time", "fulltime": "full_time", "full-time": "full_time",
    "part_time": "part_time", "parttime": "part_time", "part-time": "part_time",
    "contractor": "contract", "contract": "contract", "temporary": "contract",
    "intern": "internship", "internship": "internship",
}


def _employment_type(raw) -> str | None:
    if not isinstance(raw, str):
        return None
    return _EMPLOYMENT_TYPES.get(raw.strip().lower().replace(" ", "_"))


def _listing_job_id(url: str) -> str | None:
    """
    The posting id out of a URL, when the board did not publish one.

    This used to be "the longest number anywhere in the URL", which is the
    posting id only until something else in the URL has more digits. A date
    segment or a tracking parameter wins, and then two different postings get
    the *same* `source_job_id`:

        20250131  <-  /careers/20250131/1234
        20250131  <-  /careers/20250131/5678
       987654321  <-  /acme/j/A1B2C3?utm_campaign=987654321
       987654321  <-  /acme/j/D4E5F6?utm_campaign=987654321

    A collision there is not a duplicate that gets skipped, it is a job that is
    never stored: `find_existing_job` layer 2 matches on
    `(source, source_job_id)`, returns the first row, and treats the second
    posting as another sighting of it — appending its URL and counting it as
    `merged`. The cycle reports success.

    So: the digits a path segment *starts* with, taking the last such segment.
    Three things fall out of that.

    The query string is dropped entirely, because nothing in it identifies the
    posting. A segment has to lead with the digits, which covers both the bare
    `/jobs/778899` and Teamtailor's `/jobs/778899-backend` while rejecting a
    year that trails a slug (`/jobs/12345/engineer-2024` is 12345). And "last"
    rather than "longest", because where a date and an id are both in the path
    the id comes after it in every shape observed.

    `None` is a safe answer and the caller already guards for it: layer 2 is
    skipped and layers 1 and 3 still run.
    """
    from urllib.parse import urlsplit

    try:
        path = urlsplit(url or "").path
    except ValueError:
        return None
    found = [
        match.group(1)
        for match in (re.match(r"(\d{3,})", seg) for seg in path.split("/") if seg)
        if match
    ]
    return found[-1] if found else None


def parse_experience_level(title: str, description: str) -> str | None:
    """
    Infer seniority from job title and description text.

    Returns "entry", "senior", or None when the posting gives no signal.

    None rather than "mid", which is what this returned for years. "mid" was
    never a finding — it was the fallback, reached by matching none of the
    patterns — and returning it made two very different states identical:
    a posting that says "Mid-level Engineer" and one that says nothing at all.

    That cost three things. The jobs list filters on this column, so choosing
    "Mid" returned every posting nobody could classify. The scoring prompt
    states "Experience level: mid" as a fact about a job we know nothing about.
    And `enrich_from` could not merge the column at all — its own comment says
    why: "both ingest paths default it to 'mid' rather than leaving it null.
    There is no absence to fill, only a guess to overwrite with another guess."
    A null is an absence, so the first source that does know now fills it.

    Callers are unchanged: every one puts the result straight into an ingest
    dict, `Job.experience_level` is nullable, `take()` in `enrich_from` skips
    None, and the prompt already read `job.experience_level or 'unknown'`.
    """
    text = (title + " " + description).lower()

    senior_patterns = [
        r"\bsenior\b", r"\bsr\b", r"\blead\b", r"\bprincipal\b",
        r"\bstaff\b", r"\bdirector\b", r"\bvp\b",
    ]
    if any(re.search(p, text) for p in senior_patterns):
        return "senior"

    entry_patterns = [
        r"\bjunior\b", r"\bjr\b", r"\bentry[\s\-]level\b",
        r"\b0[\s\-]?[-–][\s\-]?[12]\s*years?\b", r"\bnew\s+grad\b",
        r"\bfresh(man|er)?\b",
    ]
    if any(re.search(p, text) for p in entry_patterns):
        return "entry"

    # No signal. Said so, rather than guessed at.
    return None
