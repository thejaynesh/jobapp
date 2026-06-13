import logging

import httpx

from app.services.sources.base import parse_experience_level

logger = logging.getLogger(__name__)

_BASE = "https://api.adzuna.com/v1/api/jobs"


def fetch(
    app_id: str,
    app_key: str,
    query: str,
    location: str,
    country: str = "us",
    results_per_page: int = 50,
) -> list[dict]:
    url = f"{_BASE}/{country}/search/1"
    params = {
        "app_id": app_id,
        "app_key": app_key,
        "what": query,
        "where": location,
        "results_per_page": results_per_page,
        "content-type": "application/json",
        "max_days_old": 1,
    }
    try:
        resp = httpx.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.error("Adzuna fetch error: %s", exc)
        return []

    jobs = []
    for item in data.get("results", []):
        job_url = item.get("redirect_url", "")
        loc = item.get("location", {}).get("display_name", "")
        title = item.get("title", "")
        desc = item.get("description", "")
        jobs.append({
            "source": "adzuna",
            "source_job_id": str(item.get("id", "")),
            "title": title,
            "company": item.get("company", {}).get("display_name", ""),
            "location": loc,
            "is_remote": "remote" in loc.lower() or "remote" in title.lower(),
            "url": job_url,
            "description": desc,
            "experience_level": parse_experience_level(title, desc),
            "posted_at": item.get("created"),
        })
    return jobs
