import logging

import httpx

from app.services.sources.base import parse_experience_level

logger = logging.getLogger(__name__)

_BASE = "https://api.adzuna.com/v1/api/jobs"


def _cfg(name: str, default):
    from app.config import settings
    return getattr(settings, name, default)


def fetch(
    app_id: str,
    app_key: str,
    query: str,
    location: str,
    country: str = "us",
    results_per_page: int = 50,
    max_pages: int | None = None,
    max_days_old: int | None = None,
) -> list[dict]:
    """
    Search one Adzuna country endpoint, newest first, across several pages.

    Note the URLs: Adzuna returns `redirect_url`, a link to its own interstitial
    rather than to the employer. services.link_resolver follows those to the real
    apply page, which is also how the company's ATS board gets discovered.
    """
    max_pages = max_pages if max_pages is not None else _cfg("ADZUNA_MAX_PAGES", 3)
    max_days_old = (
        max_days_old if max_days_old is not None else _cfg("ADZUNA_MAX_DAYS_OLD", 7)
    )

    items: list[dict] = []
    for page in range(1, max(1, max_pages) + 1):
        params = {
            "app_id": app_id,
            "app_key": app_key,
            "what": query,
            "where": location,
            "results_per_page": results_per_page,
            "content-type": "application/json",
            "sort_by": "date",
        }
        if max_days_old:
            params["max_days_old"] = max_days_old
        try:
            resp = httpx.get(f"{_BASE}/{country}/search/{page}", params=params, timeout=15)
            resp.raise_for_status()
            results = resp.json().get("results", [])
        except Exception as exc:
            logger.error("Adzuna fetch error (%s p%d): %s", country, page, exc)
            break

        items.extend(results)
        if len(results) < results_per_page:
            break

    jobs = []
    for item in items:
        job_url = item.get("redirect_url", "")
        loc = item.get("location", {}).get("display_name", "")
        title = item.get("title", "")
        desc = item.get("description", "")
        jobs.append({
            "source": "adzuna",
            "source_job_id": str(item.get("id", "")),
            "title": title,
            "company": item.get("company", {}).get("display_name", ""),
            "location": loc,
            "is_remote": "remote" in loc.lower() or "remote" in title.lower(),
            "url": job_url,
            "description": desc,
            "experience_level": parse_experience_level(title, desc),
            "posted_at": item.get("created"),
        })
    return jobs
