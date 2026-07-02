import logging

import httpx

from app.services.sources.base import parse_experience_level

logger = logging.getLogger(__name__)

_API = "https://jooble.org/api/{key}"


def fetch(api_key: str, query: str, location: str) -> list[dict]:
    """Fetch jobs from the Jooble aggregator API (free key from jooble.org/api/about)."""
    try:
        resp = httpx.post(
            _API.format(key=api_key),
            json={"keywords": query, "location": location},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.error("Jooble fetch error: %s", exc)
        return []

    jobs: list[dict] = []
    for item in data.get("jobs", []):
        title = (item.get("title") or "").strip()
        desc = item.get("snippet") or ""
        loc = (item.get("location") or "").strip()
        jobs.append({
            "source": "jooble",
            "source_job_id": str(item.get("id", "")) or None,
            "title": title,
            "company": (item.get("company") or "").strip(),
            "location": loc,
            "is_remote": "remote" in loc.lower() or "remote" in title.lower(),
            "url": item.get("link") or "",
            "description": desc,
            "experience_level": parse_experience_level(title, desc),
            "posted_at": item.get("updated"),
        })
    logger.info("Jooble: %d jobs for '%s' / '%s'", len(jobs), query, location)
    return jobs
