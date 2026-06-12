import logging

import httpx

from app.services.sources.base import parse_experience_level

logger = logging.getLogger(__name__)


def fetch(company_slugs: list[str]) -> list[dict]:
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
