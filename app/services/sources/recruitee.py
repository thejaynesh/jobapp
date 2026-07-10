import logging

import httpx

from app.services.sources.base import parse_experience_level

logger = logging.getLogger(__name__)

_API = "https://{slug}.recruitee.com/api/offers/"


def fetch(company_slugs: list[str]) -> list[dict]:
    """Fetch jobs from Recruitee's public careers API (no key; full JDs included)."""
    jobs: list[dict] = []
    for slug in company_slugs:
        try:
            resp = httpx.get(_API.format(slug=slug), timeout=15, follow_redirects=True)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.error("Recruitee fetch error for slug '%s': %s", slug, exc)
            continue

        for item in data.get("offers", []):
            title = (item.get("title") or "").strip()
            desc = item.get("description") or ""
            location = (item.get("location") or item.get("city") or "").strip()
            is_remote = bool(item.get("remote")) or "remote" in location.lower()

            jobs.append({
                "source": "recruitee",
                "source_job_id": str(item.get("id", "")),
                "title": title,
                "company": (item.get("company_name") or slug).strip(),
                "location": location,
                "is_remote": is_remote,
                "url": item.get("careers_url") or "",
                "description": desc,
                "experience_level": parse_experience_level(title, desc),
                "posted_at": item.get("created_at"),
            })
    logger.info("Recruitee: %d jobs across %d companies", len(jobs), len(company_slugs))
    return jobs
