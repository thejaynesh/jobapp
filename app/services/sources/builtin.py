"""Built In tech jobs, from structured data or public search-result cards."""

import logging
import re
from urllib.parse import quote_plus

import httpx

from app.services.descriptions import clean as clean_description
from app.services.enrichment import json_ld_postings
from app.services.sources.base import parse_experience_level, raise_if_blocked
from app.services.sources.listing_fallbacks import extract_listing_jobs

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://builtin.com/jobs"
_REMOTE_URL = "https://builtin.com/jobs/remote"

# Pages per search when the caller does not say. Twenty-five cards a page; the
# settings page overrides this (`builtin_max_pages`).
DEFAULT_MAX_PAGES = 3

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_CITY_HUBS = (
    "austin", "boston", "chicago", "colorado", "dallas",
    "los-angeles", "new-york", "san-francisco", "seattle",
)

_ID_RE = re.compile(r"/jobs?/(?:[^/]+/)?(\d+)(?:/|$)")


def _read_page(url: str) -> list[dict]:
    """
    Every posting on one page of Built In results, before any query filter.

    Raises `SourceUnavailable` on a block or a rate limit, so the fetcher stops
    asking for the rest of the cycle; any other failure is logged and reads as
    an empty page, which ends that search's paging.
    """
    try:
        resp = httpx.get(url, headers=_HEADERS, timeout=20, follow_redirects=True)
        raise_if_blocked(resp, "Built In")
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.error("BuiltIn fetch error (%s): %s", url, exc)
        return []

    postings = json_ld_postings(resp.text)
    if not postings:
        postings = extract_listing_jobs(
            resp.text, str(resp.url) if isinstance(resp.url, httpx.URL) else url,
            "builtin", "",
        )
    return postings


def _as_job(posting: dict) -> dict | None:
    title = posting["title"]
    if not title or not posting["url"]:
        return None
    desc = clean_description(posting["description"])
    location = posting["location"]
    job_url = posting["url"]
    id_match = _ID_RE.search(job_url)
    return {
        "source": "builtin",
        "source_job_id": id_match.group(1) if id_match else None,
        "title": title,
        "company": posting["company"],
        "location": location,
        "is_remote": "remote" in f"{location} {title}".lower(),
        "url": job_url,
        "description": desc,
        # The card's own seniority label where it gave one; the title otherwise.
        "experience_level": (posting.get("experience_level")
                             or parse_experience_level(title, desc)),
        "posted_at": posting["posted_at"],
        "salary_min": posting.get("salary_min"),
        "salary_max": posting.get("salary_max"),
        "salary_currency": posting.get("salary_currency"),
        "salary_period": posting.get("salary_period"),
    }


def _search(base: str, query: str, max_pages: int, seen: set[str]) -> list[dict]:
    """
    Up to `max_pages` pages of one search, stopping at the first that adds
    nothing new.

    Page one only, until this: a search for "software engineer" runs to four
    hundred pages and the adapter read the first twenty-five cards of it. The
    stop matters as much as the depth — past the last real page Built In
    serves an empty page or the first one again, and either would otherwise
    spend the rest of the budget on repeats.
    """
    q_words = set(query.lower().split())
    jobs: list[dict] = []
    # This search's own pages decide when it stops; `seen` spans both searches
    # and only decides what is returned. Stopping on `seen` would end the
    # remote search at page one whenever its first page overlapped the main
    # search, which is most of the time.
    mine: set[str] = set()
    for page in range(1, max(1, max_pages) + 1):
        url = f"{base}?search={quote_plus(query)}"
        if page > 1:
            url += f"&page={page}"
        postings = [p for p in _read_page(url) if p.get("url")]
        if not any(p["url"] not in mine for p in postings):
            break
        for posting in postings:
            mine.add(posting["url"])
            if posting["url"] in seen:
                continue
            seen.add(posting["url"])
            searchable = f"{posting['title']} {posting['company']}".lower()
            if q_words and not any(w in searchable for w in q_words):
                continue
            job = _as_job(posting)
            if job:
                jobs.append(job)
    return jobs


def fetch(query: str, max_pages: int | None = None) -> list[dict]:
    """Fetch tech jobs from Built In's search and its remote search."""
    pages = DEFAULT_MAX_PAGES if max_pages is None else int(max_pages)
    seen: set[str] = set()
    all_jobs = _search(_SEARCH_URL, query, pages, seen)
    all_jobs += _search(_REMOTE_URL, query, pages, seen)
    logger.info("BuiltIn: %d jobs for query '%s'", len(all_jobs), query)
    return all_jobs
