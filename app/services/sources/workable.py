import logging

import httpx

from app.services.sources.base import parse_experience_level

logger = logging.getLogger(__name__)

_API = "https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true"


def fetch(company_slugs: list[str]) -> list[dict]:
    """Fetch jobs from Workable's public widget API (no key; details=true includes full JDs)."""
    jobs: list[dict] = []
    for slug in company_slugs:
        try:
            resp = httpx.get(_API.format(slug=slug), timeout=15, follow_redirects=True)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.error("Workable fetch error for slug '%s': %s", slug, exc)
            continue

        company = (data.get("name") or slug).strip()
        for item in data.get("jobs", []):
            title = (item.get("title") or "").strip()
            desc = item.get("description") or ""
            location_parts = [item.get("city"), item.get("state"), item.get("country")]
            location = ", ".join(p for p in location_parts if p)
            is_remote = bool(item.get("telecommuting")) or "remote" in location.lower()

            jobs.append({
                "source": "workable",
                "source_job_id": item.get("shortcode"),
                "title": title,
                "company": company,
                "location": location,
                "is_remote": is_remote,
                "url": item.get("url") or f"https://apply.workable.com/{slug}/",
                "description": desc,
                "experience_level": parse_experience_level(title, desc),
                "posted_at": item.get("published_on"),
            })
    logger.info("Workable: %d jobs across %d companies", len(jobs), len(company_slugs))
    return jobs
