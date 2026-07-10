import logging
from datetime import datetime, timezone, timedelta

import httpx

from app.config import settings
from app.services.sources.base import parse_experience_level

logger = logging.getLogger(__name__)


def _cutoff() -> datetime:
    # Align with the fetcher's freshness window: a 25h cutoff hid every existing
    # opening at newly configured/discovered companies. Dedupe absorbs re-fetches.
    days = getattr(settings, "MAX_JOB_AGE_DAYS", 30) or 30
    return datetime.now(timezone.utc) - timedelta(days=days)


def fetch(company_slugs: list[str]) -> list[dict]:
    cutoff = _cutoff()
    jobs = []
    for slug in company_slugs:
        url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
        try:
            resp = httpx.get(url, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.error("Greenhouse fetch error for slug '%s': %s", slug, exc)
            continue

        for item in data.get("jobs", []):
            updated_raw = item.get("updated_at", "")
            if updated_raw:
                try:
                    updated = datetime.fromisoformat(updated_raw.replace("Z", "+00:00"))
                    if updated < cutoff:
                        continue
                except Exception:
                    pass
            title = item.get("title", "")
            desc = item.get("content", "")
            loc = item.get("location", {}).get("name", "")
            jobs.append({
                "source": "greenhouse",
                "source_job_id": str(item.get("id", "")),
                "title": title,
                "company": slug,
                "location": loc,
                "is_remote": "remote" in loc.lower() or "remote" in title.lower(),
                "url": item.get("absolute_url", ""),
                "description": desc,
                "experience_level": parse_experience_level(title, desc),
                "posted_at": item.get("updated_at"),
            })
    return jobs
