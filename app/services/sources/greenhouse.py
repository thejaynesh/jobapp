import logging
from datetime import datetime, timezone, timedelta

import httpx

from app.services.sources.base import parse_experience_level

logger = logging.getLogger(__name__)

_CUTOFF_HOURS = 25  # slightly over 24h to avoid missing jobs on boundary


def fetch(company_slugs: list[str]) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=_CUTOFF_HOURS)
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
            })
    return jobs
