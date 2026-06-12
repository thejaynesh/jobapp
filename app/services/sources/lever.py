import logging

import httpx

from app.services.sources.base import parse_experience_level

logger = logging.getLogger(__name__)


def fetch(company_slugs: list[str]) -> list[dict]:
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
            })
    return jobs
