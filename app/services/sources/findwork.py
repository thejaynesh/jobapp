import logging

import httpx

from app.services.sources.base import parse_experience_level

logger = logging.getLogger(__name__)

_API = "https://findwork.dev/api/jobs/"


def fetch(api_key: str, query: str) -> list[dict]:
    """Fetch developer jobs from Findwork (free key from findwork.dev/developers)."""
    try:
        resp = httpx.get(
            _API,
            params={"search": query, "sort_by": "date"},
            headers={"Authorization": f"Token {api_key}"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.error("Findwork fetch error: %s", exc)
        return []

    jobs: list[dict] = []
    for item in data.get("results", []):
        title = (item.get("role") or "").strip()
        desc = item.get("text") or ""
        location = (item.get("location") or "").strip()
        is_remote = bool(item.get("remote")) or "remote" in location.lower()
        jobs.append({
            "source": "findwork",
            "source_job_id": str(item.get("id", "")) or None,
            "title": title,
            "company": (item.get("company_name") or "").strip(),
            "location": location or ("Remote" if is_remote else ""),
            "is_remote": is_remote,
            "url": item.get("url") or "",
            "description": desc,
            "experience_level": parse_experience_level(title, desc),
            "posted_at": item.get("date_posted"),
        })
    logger.info("Findwork: %d jobs for '%s'", len(jobs), query)
    return jobs
