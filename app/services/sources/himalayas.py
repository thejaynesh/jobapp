import logging

import httpx

from app.services.sources.base import parse_experience_level

logger = logging.getLogger(__name__)

_BASE = "https://himalayas.app/jobs/api"


def fetch(query: str) -> list[dict]:
    """Fetch remote tech jobs from Himalayas' free public API (no key required)."""
    try:
        resp = httpx.get(_BASE, params={"limit": 100}, timeout=15, follow_redirects=True)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.error("Himalayas fetch error: %s", exc)
        return []

    q_words = set(query.lower().split())
    jobs: list[dict] = []
    seen: set[str] = set()

    for item in data.get("jobs", []):
        title = (item.get("title") or "").strip()
        categories = " ".join(item.get("categories") or []).lower()
        searchable = (title + " " + categories).lower()
        if q_words and not any(w in searchable for w in q_words):
            continue

        url = item.get("applicationLink") or item.get("guid") or ""
        job_id = str(item.get("guid") or url)
        if not job_id or job_id in seen:
            continue
        seen.add(job_id)

        desc = item.get("description") or item.get("excerpt") or ""
        location_restrictions = item.get("locationRestrictions") or []
        location = ", ".join(location_restrictions) if location_restrictions else "Remote"

        # pubDate is a unix timestamp (sometimes as a string); normalize to int
        posted_at = item.get("pubDate")
        try:
            posted_at = int(posted_at)
        except (TypeError, ValueError):
            posted_at = None

        jobs.append({
            "source": "himalayas",
            "source_job_id": job_id,
            "title": title,
            "company": (item.get("companyName") or "").strip(),
            "location": location,
            "is_remote": True,
            "url": url,
            "description": desc,
            "experience_level": parse_experience_level(title, desc),
            "posted_at": posted_at,
        })

    logger.info("Himalayas: %d jobs for query '%s'", len(jobs), query)
    return jobs
