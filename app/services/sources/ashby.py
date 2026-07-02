import logging
import re
from datetime import datetime, timezone, timedelta

import httpx

from app.services.sources.base import parse_experience_level

logger = logging.getLogger(__name__)

_BASE = "https://jobs.ashbyhq.com/api/non-user-facing/posting-board/job-board/jobs"


def _strip_html(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html).strip()


def fetch(company_slugs: list[str]) -> list[dict]:
    # Align with the fetcher's freshness window (was 25h, which hid every
    # existing opening at newly configured/discovered companies).
    from app.config import settings
    days = getattr(settings, "MAX_JOB_AGE_DAYS", 30) or 30
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    jobs = []
    for slug in company_slugs:
        params = {"organizationHostedJobsPageName": slug}
        try:
            resp = httpx.get(_BASE, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.error("Ashby fetch error for slug '%s': %s", slug, exc)
            continue

        for item in data.get("jobPostings", []):
            published_raw = item.get("publishedDate", "")
            if published_raw:
                try:
                    published = datetime.fromisoformat(published_raw.replace("Z", "+00:00"))
                    if published < cutoff:
                        continue
                except Exception:
                    pass
            title = item.get("title", "")
            desc = _strip_html(item.get("descriptionHtml", ""))
            loc = item.get("locationName", "")
            jobs.append({
                "source": "ashby",
                "source_job_id": item.get("id"),
                "title": title,
                "company": slug,
                "location": loc,
                "is_remote": bool(item.get("isRemote", False)),
                "url": item.get("jobUrl", ""),
                "description": desc,
                "experience_level": parse_experience_level(title, desc),
                "posted_at": published_raw or None,
            })
    return jobs
