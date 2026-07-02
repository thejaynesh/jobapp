import logging
from datetime import datetime, timezone, timedelta

import httpx

from app.config import settings
from app.services.sources.base import parse_experience_level

logger = logging.getLogger(__name__)


def fetch(company_slugs: list[str]) -> list[dict]:
    # Align with the fetcher's freshness window (was 25h, which hid every
    # existing opening at newly configured/discovered companies).
    days = getattr(settings, "MAX_JOB_AGE_DAYS", 30) or 30
    cutoff_ms = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000
    jobs = []
    for slug in company_slugs:
        url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
        try:
            resp = httpx.get(url, timeout=15)
            resp.raise_for_status()
            items = resp.json()
        except Exception as exc:
            logger.error("Lever fetch error for slug '%s': %s", slug, exc)
            continue

        for item in items:
            created_at = item.get("createdAt", 0)
            if created_at and created_at < cutoff_ms:
                continue
            title = item.get("text", "")
            desc = item.get("descriptionPlain", "")
            loc = item.get("categories", {}).get("location", "")
            jobs.append({
                "source": "lever",
                "source_job_id": item.get("id"),
                "title": title,
                "company": slug,
                "location": loc,
                "is_remote": "remote" in loc.lower() or "remote" in title.lower(),
                "url": item.get("hostedUrl", ""),
                "description": desc,
                "experience_level": parse_experience_level(title, desc),
                "posted_at": created_at / 1000 if created_at else None,
            })
    return jobs
