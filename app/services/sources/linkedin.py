"""
LinkedIn jobs via the public guest API (no browser, no login required).

Volume notes — the guest search endpoint returns ~10 cards per request, so a
single `start=0` call caps the whole source at ten jobs per query/location.
This adapter paginates, parses each card independently (positional zipping of
four separate regex sweeps silently mismatched company/location whenever a card
omitted a field), and enriches descriptions concurrently across the *whole*
cycle rather than per search — the same posting shows up under many
query/location combinations and only needs fetching once.

Descriptions matter disproportionately here: the downstream skill filter rejects
any job with an empty description, so the detail-fetch budget is effectively the
source's real yield ceiling.
"""

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import httpx

from app.services.sources.base import parse_experience_level
from app.services.sources.playwright_base import encode, is_remote_location

logger = logging.getLogger(__name__)

_GUEST_API = (
    "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
    "?keywords={query}&location={location}&start={start}&sortBy=DD{recency}"
)
_POSTING_API = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# The guest endpoint pages in blocks of 10.
_PAGE_SIZE = 10
_DEFAULT_MAX_PAGES = 5
_DEFAULT_RECENCY_HOURS = 168  # 7 days — the fetch cycle runs every few hours
_DEFAULT_MAX_DETAILS = 200    # cycle-wide budget, not per search
_DEFAULT_DETAIL_WORKERS = 4

_SEARCH_PAUSE_SECONDS = 0.4
# LinkedIn throttles guest traffic aggressively; back off rather than burn the
# rest of the cycle on 429s.
_MAX_CONSECUTIVE_THROTTLES = 3
_THROTTLE_BACKOFF_SECONDS = 5

# Descriptions never change, and the same posting recurs across queries and
# cycles, so cache them for the life of the worker process. The posting date
# rides along: it comes from the same response.
_DESC_CACHE: dict[str, tuple[str, str | None]] = {}
_DESC_CACHE_MAX = 5000


def _cache_description(job_id: str, description: str,
                       posted_at: str | None = None) -> None:
    if len(_DESC_CACHE) >= _DESC_CACHE_MAX:
        for stale in list(_DESC_CACHE)[: _DESC_CACHE_MAX // 5]:
            _DESC_CACHE.pop(stale, None)
    _DESC_CACHE[job_id] = (description, posted_at)


def _cfg(name: str, default):
    from app.config import settings
    return getattr(settings, name, default)


def _strip(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html).strip()


def _job_id_from_url(url: str) -> str | None:
    """Job view URLs end in a numeric posting id: .../jobs/view/some-title-4012345678"""
    m = re.search(r"(\d{8,})/?$", url)
    return m.group(1) if m else None


class _Throttled(Exception):
    """LinkedIn returned 429 for a guest request."""


class _Unparsed(Exception):
    """LinkedIn answered with content we couldn't find any job cards in."""


# Below this, an empty response is just an exhausted result set, not a problem.
_MIN_MEANINGFUL_RESPONSE = 500


# --- card parsing ----------------------------------------------------------

_CARD_URL_RE = re.compile(
    r'href="(https://(?:[a-z]{2,3}\.)?linkedin\.com/jobs/view/[^"?]+)', re.I
)
_CARD_URN_RE = re.compile(r'data-entity-urn="urn:li:jobPosting:(\d+)"', re.I)
_CARD_TITLE_RE = re.compile(
    r'class="base-search-card__title[^"]*"[^>]*>(.*?)</h3>', re.DOTALL
)
_CARD_COMPANY_RE = re.compile(
    r'class="base-search-card__subtitle[^"]*"[^>]*>.*?<[^>]+>(.*?)</', re.DOTALL
)
_CARD_COMPANY_FALLBACK_RE = re.compile(
    r'class="[^"]*hidden-nested-link[^"]*"[^>]*>(.*?)</a>', re.DOTALL
)
_CARD_LOCATION_RE = re.compile(
    r'class="job-search-card__location[^"]*"[^>]*>(.*?)</span>', re.DOTALL
)
# A time component is allowed to follow: requiring the closing quote straight
# after the date silently dropped any card that carried one, and a card with no
# date at all is not filtered for age by the fetcher.
_CARD_DATE_RE = re.compile(r'datetime="(\d{4}-\d{2}-\d{2})(?:[T ][^"]*)?"')


def _split_cards(html: str) -> list[str]:
    """One <li> per search result; keep the chunks so fields can't cross cards."""
    chunks = re.split(r"<li\b", html)
    return chunks[1:] if len(chunks) > 1 else ([html] if html.strip() else [])


def _parse_card(card: str) -> dict | None:
    url_match = _CARD_URL_RE.search(card)
    title_match = _CARD_TITLE_RE.search(card)
    if not url_match or not title_match:
        return None

    title = _strip(title_match.group(1))
    if not title:
        return None

    url = url_match.group(1)
    urn_match = _CARD_URN_RE.search(card)
    job_id = urn_match.group(1) if urn_match else _job_id_from_url(url)

    company_match = _CARD_COMPANY_RE.search(card) or _CARD_COMPANY_FALLBACK_RE.search(card)
    location_match = _CARD_LOCATION_RE.search(card)
    date_match = _CARD_DATE_RE.search(card)

    location = _strip(location_match.group(1)) if location_match else ""
    return {
        "source": "linkedin",
        "source_job_id": job_id,
        "title": title,
        "company": _strip(company_match.group(1)) if company_match else "",
        "location": location,
        "is_remote": is_remote_location(location, title),
        "url": url,
        "description": "",
        "experience_level": parse_experience_level(title, ""),
        "posted_at": date_match.group(1) if date_match else None,
    }


# --- network ---------------------------------------------------------------

def _search_page(query: str, location: str, start: int, headers: dict,
                 recency_hours: int) -> list[dict]:
    recency = f"&f_TPR=r{recency_hours * 3600}" if recency_hours else ""
    url = _GUEST_API.format(
        query=encode(query), location=encode(location), start=start, recency=recency
    )
    resp = httpx.get(url, headers=headers, timeout=15, follow_redirects=True)
    if resp.status_code == 429:
        raise _Throttled(f"429 on start={start}")
    resp.raise_for_status()

    cards = [_parse_card(card) for card in _split_cards(resp.text)]
    parsed = [c for c in cards if c]
    # A substantial 200 that yields nothing means the markup moved, which is a
    # very different problem from "no jobs matched" — say so.
    if not parsed and len(resp.text) > _MIN_MEANINGFUL_RESPONSE:
        raise _Unparsed(
            f"{len(resp.text)} bytes returned but no job cards parsed "
            f"(LinkedIn markup may have changed)"
        )
    return parsed


def _fetch_description(job_id: str, headers: dict) -> tuple[str, str | None]:
    """The JD and posting date from the guest job-posting endpoint."""
    if job_id in _DESC_CACHE:
        return _DESC_CACHE[job_id]
    try:
        resp = httpx.get(
            _POSTING_API.format(job_id=job_id),
            headers=headers, timeout=15, follow_redirects=True,
        )
        if resp.status_code == 429:
            raise _Throttled(f"429 on posting {job_id}")
        resp.raise_for_status()
        html = resp.text
    except _Throttled:
        raise
    except Exception as exc:
        logger.warning("LinkedIn posting fetch error (%s): %s", job_id, exc)
        return "", None

    text = _extract_description(html)
    posted_at = _extract_posted_at(html)
    _cache_description(job_id, text, posted_at)
    return text, posted_at


_MARKUP_RE = re.compile(
    r'class="show-more-less-html__markup[^"]*"[^>]*>(.*?)</div>', re.DOTALL
)
_DESC_TEXT_RE = re.compile(
    r'class="description__text[^"]*"[^>]*>(.*?)</section>', re.DOTALL
)

# The posting page carries the date in up to three forms. Search cards often
# carry none at all, and an undated job isn't age-filtered — so a year-old
# posting that stopped taking applications lands in the list looking fresh.
# This page is already being fetched for the description; the date is free.
_LD_DATE_RE = re.compile(r'"datePosted"\s*:\s*"(\d{4}-\d{2}-\d{2})')
_POSTING_DATETIME_RE = re.compile(r'datetime="(\d{4}-\d{2}-\d{2})(?:[T ][^"]*)?"')
_AGO_RE = re.compile(
    r"(\d+)\s+(minute|hour|day|week|month|year)s?\s+ago", re.I
)
_AGO_DAYS = {"minute": 0, "hour": 0, "day": 1, "week": 7, "month": 30, "year": 365}


def _extract_posted_at(html: str) -> str | None:
    """
    The posting date as YYYY-MM-DD, or None.

    Exact dates win. The "3 weeks ago" phrase is a fallback because it's what
    the guest fragment usually shows, and an approximate date is far better
    than none: none means the age filter is skipped entirely.
    """
    for pattern in (_LD_DATE_RE, _POSTING_DATETIME_RE):
        match = pattern.search(html)
        if match:
            return match.group(1)

    match = _AGO_RE.search(html)
    if not match:
        return None
    days = int(match.group(1)) * _AGO_DAYS[match.group(2).lower()]
    return (date.today() - timedelta(days=days)).isoformat()


def _extract_description(html: str) -> str:
    match = _MARKUP_RE.search(html) or _DESC_TEXT_RE.search(html)
    if not match:
        return ""
    # Keep paragraph/bullet boundaries as newlines so the text stays readable.
    text = re.sub(r"<(?:br|/p|/li|/ul)[^>]*>", "\n", match.group(1))
    return re.sub(r"\n{3,}", "\n\n", _strip(text))


# --- public API ------------------------------------------------------------

def fetch_all(
    session_cookie: str,
    queries: list[str],
    locations: list[str],
    max_pages: int | None = None,
    recency_hours: int | None = None,
    max_details: int | None = None,
    detail_workers: int | None = None,
) -> list[dict]:
    """
    Search every (query, location) pair, deduplicate by posting id across the
    whole cycle, then fill in descriptions for as many postings as the budget
    allows (newest first, since the search is date-sorted).
    """
    max_pages = max_pages if max_pages is not None else _cfg("LINKEDIN_MAX_PAGES", _DEFAULT_MAX_PAGES)
    recency_hours = (
        recency_hours if recency_hours is not None
        else _cfg("LINKEDIN_RECENCY_HOURS", _DEFAULT_RECENCY_HOURS)
    )
    max_details = (
        max_details if max_details is not None
        else _cfg("LINKEDIN_MAX_DETAIL_FETCHES", _DEFAULT_MAX_DETAILS)
    )
    detail_workers = (
        detail_workers if detail_workers is not None
        else _cfg("LINKEDIN_DETAIL_WORKERS", _DEFAULT_DETAIL_WORKERS)
    )

    headers = dict(_HEADERS)
    if session_cookie:
        headers["Cookie"] = f"li_at={session_cookie}"

    by_id: dict[str, dict] = {}
    without_id: list[dict] = []
    throttles = 0
    unparsed = 0
    searches = 0

    for query in queries:
        for location in locations:
            if throttles >= _MAX_CONSECUTIVE_THROTTLES:
                break
            for page in range(max_pages):
                if searches:
                    time.sleep(_SEARCH_PAUSE_SECONDS)
                searches += 1
                try:
                    cards = _search_page(
                        query, location, page * _PAGE_SIZE, headers, recency_hours
                    )
                except _Throttled as exc:
                    throttles += 1
                    logger.warning("LinkedIn throttled (%s/%s): %s",
                                   throttles, _MAX_CONSECUTIVE_THROTTLES, exc)
                    time.sleep(_THROTTLE_BACKOFF_SECONDS * throttles)
                    break
                except _Unparsed as exc:
                    # Worth one loud line, not one per query — the markup is
                    # either right for every search or wrong for every search.
                    unparsed += 1
                    if unparsed == 1:
                        logger.error("LinkedIn: %s", exc)
                    break
                except Exception as exc:
                    logger.error("LinkedIn guest API error (%s / %s): %s", query, location, exc)
                    break

                throttles = 0
                for card in cards:
                    job_id = card["source_job_id"]
                    if job_id:
                        by_id.setdefault(job_id, card)
                    else:
                        without_id.append(card)

                # A short page means the result set is exhausted.
                if len(cards) < _PAGE_SIZE:
                    break
        if throttles >= _MAX_CONSECUTIVE_THROTTLES:
            logger.error("LinkedIn: giving up this cycle after repeated throttling")
            break

    jobs = list(by_id.values()) + without_id
    _enrich_descriptions(jobs, headers, max_details, detail_workers)

    with_desc = sum(1 for j in jobs if j["description"])
    undated = sum(1 for j in jobs if not j.get("posted_at"))
    logger.info(
        "LinkedIn: %d unique jobs from %d searches (%d with descriptions, "
        "%d with no posting date)",
        len(jobs), searches, with_desc, undated,
    )
    return jobs


def _enrich_descriptions(jobs: list[dict], headers: dict, max_details: int,
                         workers: int) -> None:
    """Fetch JDs in parallel for the first `max_details` postings, in place."""
    targets = [j for j in jobs if j["source_job_id"]][:max_details]
    if not targets:
        return

    def _one(job: dict) -> None:
        try:
            description, posted_at = _fetch_description(job["source_job_id"], headers)
        except _Throttled as exc:
            logger.warning("LinkedIn description throttled: %s", exc)
            raise
        job["description"] = description
        # The card's own date wins when it has one — it's exact, whereas the
        # posting page may only offer "3 weeks ago".
        if posted_at and not job.get("posted_at"):
            job["posted_at"] = posted_at
        job["experience_level"] = parse_experience_level(job["title"], job["description"])

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(targets)))) as pool:
        futures = [pool.submit(_one, job) for job in targets]
        throttled = 0
        for future in futures:
            try:
                future.result()
            except _Throttled:
                throttled += 1
            except Exception as exc:
                logger.warning("LinkedIn description error: %s", exc)
        if throttled:
            logger.warning("LinkedIn: %d description fetches throttled", throttled)


def fetch(session_cookie: str, query: str, location: str) -> list[dict]:
    """Single query/location search — kept for callers that fetch one at a time."""
    return fetch_all(session_cookie, [query], [location])
