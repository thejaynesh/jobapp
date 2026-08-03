import logging
from datetime import datetime, timezone, timedelta

import httpx

from app.services.sources.base import (
    board_workers,
    fetch_boards_concurrently,
    parse_experience_level,
)

logger = logging.getLogger(__name__)

# Ashby's documented public posting API. (The previous internal
# "non-user-facing" endpoint began returning 404 for every organization.)
_BASE = "https://api.ashbyhq.com/posting-api/job-board/{slug}"


def fetch(company_slugs: list[str]) -> list[dict]:
    from app.config import settings
    days = getattr(settings, "MAX_JOB_AGE_DAYS", 30) or 30
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    def _fetch_one(slug: str) -> list[dict]:
        resp = httpx.get(_BASE.format(slug=slug), timeout=15)
        resp.raise_for_status()
        data = resp.json()

        jobs = []
        for item in data.get("jobs", []):
            if item.get("isListed") is False:
                continue
            published_raw = item.get("publishedAt", "")
            if published_raw:
                try:
                    published = datetime.fromisoformat(published_raw.replace("Z", "+00:00"))
                    if published < cutoff:
                        continue
                except Exception:
                    pass
            title = item.get("title", "")
            desc = item.get("descriptionPlain") or ""
            loc = item.get("location", "")
            jobs.append({
                "source": "ashby",
                "source_job_id": item.get("id"),
                "title": title,
                "company": slug,
                "location": loc,
                "is_remote": bool(item.get("isRemote", False)),
                "url": item.get("jobUrl") or item.get("applyUrl") or "",
                "description": desc,
                "experience_level": parse_experience_level(title, desc),
                "posted_at": published_raw or None,
            })
        return jobs

    return fetch_boards_concurrently(company_slugs, _fetch_one, "Ashby", board_workers())
