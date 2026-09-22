"""
Built In — tech-focused job board with city hubs (builtin.com).

Built In publishes JobPosting structured data on every search results page,
the same data Google's job search reads. One request per city page returns
up to 100 tech jobs with titles, companies, locations, and descriptions —
all server-rendered, no browser needed.

No API key, no scraping of DOM elements, no Playwright: just the JSON-LD
blocks every employer pays to have on their listing. A redesign that changes
the card markup has no effect; only removing structured data entirely would
break this, and that would cost every employer their Google Jobs placement.
"""

import logging
import re
from urllib.parse import quote_plus

import httpx

from app.services.descriptions import clean as clean_description
from app.services.enrichment import json_ld_postings
from app.services.sources.base import parse_experience_level

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://builtin.com/jobs"
_REMOTE_URL = "https://builtin.com/jobs/remote"

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

_ID_RE = re.compile(r"/jobs?/(\d+)")


def _fetch_page(url: str, query: str) -> list[dict]:
    """One page of Built In results as job dicts."""
    try:
        resp = httpx.get(url, headers=_HEADERS, timeout=20, follow_redirects=True)
        resp.raise_for_status()
    except Exception as exc:
        logger.error("BuiltIn fetch error (%s): %s", url, exc)
        return []

    postings = json_ld_postings(resp.text)
    if not postings:
        return []

    q_lower = query.lower()
    q_words = set(q_lower.split())
    jobs: list[dict] = []

    for posting in postings:
        title = posting["title"]
        company = posting["company"]
        if not title or not posting["url"]:
            continue

        searchable = f"{title} {company}".lower()
        if q_words and not any(w in searchable for w in q_words):
            continue

        desc = clean_description(posting["description"])
        location = posting["location"]
        job_url = posting["url"]

        id_match = _ID_RE.search(job_url)
        source_job_id = id_match.group(1) if id_match else None

        jobs.append({
            "source": "builtin",
            "source_job_id": source_job_id,
            "title": title,
            "company": company,
            "location": location,
            "is_remote": "remote" in f"{location} {title}".lower(),
            "url": job_url,
            "description": desc,
            "experience_level": parse_experience_level(title, desc),
            "posted_at": posting["posted_at"],
            "salary_min": posting["salary_min"],
            "salary_max": posting["salary_max"],
            "salary_currency": posting["salary_currency"],
        })

    return jobs


def fetch(query: str) -> list[dict]:
    """Fetch tech jobs from Built In's search and city hub pages."""
    seen_urls: set[str] = set()
    all_jobs: list[dict] = []

    search_url = f"{_SEARCH_URL}?search={quote_plus(query)}"
    for job in _fetch_page(search_url, query):
        if job["url"] not in seen_urls:
            seen_urls.add(job["url"])
            all_jobs.append(job)

    remote_url = f"{_REMOTE_URL}?search={quote_plus(query)}"
    for job in _fetch_page(remote_url, query):
        if job["url"] not in seen_urls:
            seen_urls.add(job["url"])
            all_jobs.append(job)

    logger.info("BuiltIn: %d jobs for query '%s'", len(all_jobs), query)
    return all_jobs
