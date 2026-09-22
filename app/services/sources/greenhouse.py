import logging
from datetime import datetime

import httpx

from app.services.sources.base import (
    age_cutoff,
    board_workers,
    fetch_boards_concurrently,
    parse_experience_level,
)

logger = logging.getLogger(__name__)


def fetch(company_slugs: list[str], max_age_days=None) -> list[dict]:
    cutoff = age_cutoff(max_age_days)

    def _fetch_one(slug: str) -> list[dict]:
        url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
        resp = httpx.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        jobs = []
        for item in data.get("jobs", []):
            # `first_published` is when the posting went up; `updated_at` moves
            # on every edit, so an old requisition someone re-saved looked new
            # and a posting's age read as the age of its last typo fix.
            dated_raw = item.get("first_published") or item.get("updated_at") or ""
            if dated_raw and cutoff is not None:
                try:
                    dated = datetime.fromisoformat(dated_raw.replace("Z", "+00:00"))
                    if dated < cutoff:
                        continue
                except Exception:
                    pass
            title = item.get("title", "")
            desc = item.get("content", "")
            loc = (item.get("location") or {}).get("name", "")
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
                "posted_at": dated_raw or None,
            })
        return jobs

    return fetch_boards_concurrently(
        company_slugs, _fetch_one, "Greenhouse", board_workers()
    )
