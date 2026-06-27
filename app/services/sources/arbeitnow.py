import logging

import httpx

from app.services.sources.base import parse_experience_level

logger = logging.getLogger(__name__)

_BASE = "https://arbeitnow.com/api/job-board-api"


def fetch(query: str, location: str) -> list[dict]:
    """Fetch jobs from Arbeitnow's free public API (primarily remote/EU tech roles)."""
    try:
        resp = httpx.get(_BASE, params={"page": 1}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.error("Arbeitnow fetch error: %s", exc)
        return []

    q_words = set(query.lower().split())
    jobs: list[dict] = []

    for item in data.get("data", []):
        title = (item.get("title") or "").strip()
        tags_text = " ".join(item.get("tags") or []).lower()
        searchable = (title + " " + tags_text).lower()

        if q_words and not q_words.intersection(searchable.split()):
            if not any(w in searchable for w in q_words):
                continue

        desc = item.get("description") or ""
        jobs.append({
            "source": "arbeitnow",
            "source_job_id": item.get("slug"),
            "title": title,
            "company": (item.get("company_name") or "").strip(),
            "location": (item.get("location") or location).strip(),
            "is_remote": bool(item.get("remote", False)),
            "url": item.get("url") or "",
            "description": desc,
            "experience_level": parse_experience_level(title, desc),
            "posted_at": item.get("created_at"),
        })

    logger.info("Arbeitnow: %d jobs for query '%s'", len(jobs), query)
    return jobs
