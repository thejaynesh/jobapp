import logging

import httpx

from app.services.sources.base import (
    age_cutoff,
    board_workers,
    fetch_boards_concurrently,
    parse_experience_level,
)

logger = logging.getLogger(__name__)


def fetch(company_slugs: list[str], max_age_days=None) -> list[dict]:
    # The fetcher's freshness window, as set on the settings page.
    cutoff = age_cutoff(max_age_days)
    cutoff_ms = cutoff.timestamp() * 1000 if cutoff is not None else 0

    def _fetch_one(slug: str) -> list[dict]:
        url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
        resp = httpx.get(url, timeout=15)
        resp.raise_for_status()
        items = resp.json()

        jobs = []
        for item in items:
            created_at = item.get("createdAt", 0)
            if created_at and created_at < cutoff_ms:
                continue
            title = item.get("text", "")
            desc = item.get("descriptionPlain", "")
            loc = (item.get("categories") or {}).get("location", "")
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

    return fetch_boards_concurrently(company_slugs, _fetch_one, "Lever", board_workers())
