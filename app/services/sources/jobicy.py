import logging

import httpx

from app.services.sources.base import parse_experience_level

logger = logging.getLogger(__name__)

_BASE = "https://jobicy.com/api/v2/remote-jobs"


def fetch(query: str) -> list[dict]:
    """Fetch remote tech jobs from Jobicy's free public API (no key required)."""
    try:
        resp = httpx.get(
            _BASE,
            params={"count": 50, "tag": query},
            timeout=15,
            follow_redirects=True,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.error("Jobicy fetch error: %s", exc)
        return []

    jobs: list[dict] = []
    for item in data.get("jobs", []):
        job_id = str(item.get("id", ""))
        if not job_id:
            continue

        title = (item.get("jobTitle") or "").strip()
        desc = item.get("jobDescription") or item.get("jobExcerpt") or ""
        level = (item.get("jobLevel") or "").strip()

        jobs.append({
            "source": "jobicy",
            "source_job_id": job_id,
            "title": title,
            "company": (item.get("companyName") or "").strip(),
            "location": (item.get("jobGeo") or "Remote").strip(),
            "is_remote": True,
            "url": item.get("url") or "",
            "description": desc,
            "experience_level": parse_experience_level(f"{title} {level}", desc),
            "posted_at": item.get("pubDate"),
        })

    logger.info("Jobicy: %d jobs for query '%s'", len(jobs), query)
    return jobs
