"""
Working Nomads — free public API for remote jobs across all categories.

No API key required. The endpoint returns a JSON array of remote job postings
spanning tech, marketing, design, management and other categories. Each posting
includes a full description, which is rare among free sources and means these
skip the enrichment queue entirely.
"""

import logging

import httpx

from app.services.descriptions import clean as clean_description
from app.services.sources.base import parse_experience_level

logger = logging.getLogger(__name__)

_API = "https://www.workingnomads.com/api/exposed_jobs/"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def fetch(query: str) -> list[dict]:
    """Fetch remote jobs from Working Nomads' free public API."""
    try:
        resp = httpx.get(_API, headers=_HEADERS, timeout=20, follow_redirects=True)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.error("WorkingNomads fetch error: %s", exc)
        return []

    if not isinstance(data, list):
        logger.warning("WorkingNomads: unexpected response type %s", type(data).__name__)
        return []

    q_lower = query.lower()
    q_words = set(q_lower.split())
    jobs: list[dict] = []

    for item in data:
        if not isinstance(item, dict):
            continue

        title = (item.get("title") or "").strip()
        if not title:
            continue

        company = (item.get("company_name") or "").strip()
        category = (item.get("category_name") or "").lower()
        tags = " ".join(item.get("tags") or "").lower() if isinstance(item.get("tags"), str) else ""
        searchable = f"{title} {company} {category} {tags}".lower()

        if q_words and not any(w in searchable for w in q_words):
            continue

        url = (item.get("url") or "").strip()
        if not url:
            continue

        desc_raw = item.get("description") or ""
        desc = clean_description(desc_raw)
        location = (item.get("location") or "Remote").strip()

        jobs.append({
            "source": "workingnomads",
            "source_job_id": str(item.get("id", "")) or None,
            "title": title,
            "company": company,
            "location": location,
            "is_remote": True,
            "url": url,
            "description": desc,
            "experience_level": parse_experience_level(title, desc),
            "posted_at": item.get("pub_date"),
        })

    logger.info("WorkingNomads: %d jobs for query '%s'", len(jobs), query)
    return jobs
