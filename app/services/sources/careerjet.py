import logging

import httpx

from app.services.sources.base import parse_experience_level

logger = logging.getLogger(__name__)

_API = "http://public-api.careerjet.net/search"

# Careerjet indexes per-locale; pick the locale from the search location.
_LOCALE_HINTS = [
    ("en_GB", ("united kingdom", "london", "uk", "england")),
    ("en_CA", ("canada", "toronto", "vancouver", "montreal")),
    ("en_AU", ("australia", "sydney", "melbourne")),
    ("en_IN", ("india", "bengaluru", "bangalore", "mumbai")),
]


def _locale_for(location: str) -> str:
    low = (location or "").lower()
    for locale, hints in _LOCALE_HINTS:
        if any(h in low for h in hints):
            return locale
    return "en_US"


def fetch(affid: str, query: str, location: str) -> list[dict]:
    """Fetch jobs from the Careerjet aggregator (free affiliate id from careerjet.com)."""
    try:
        resp = httpx.get(
            _API,
            params={
                "keywords": query,
                "location": location,
                "affid": affid,
                "locale_code": _locale_for(location),
                "sort": "date",
                "pagesize": 50,
                # required by the API for affiliate accounting
                "user_ip": "127.0.0.1",
                "user_agent": "jobapp/1.0",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.error("Careerjet fetch error: %s", exc)
        return []

    if data.get("type") != "JOBS":
        logger.warning("Careerjet returned no jobs payload: %s", str(data)[:120])
        return []

    jobs: list[dict] = []
    for item in data.get("jobs", []):
        title = (item.get("title") or "").strip()
        desc = item.get("description") or ""
        loc = (item.get("locations") or "").strip()
        jobs.append({
            "source": "careerjet",
            "source_job_id": None,
            "title": title,
            "company": (item.get("company") or "").strip(),
            "location": loc,
            "is_remote": "remote" in loc.lower() or "remote" in title.lower(),
            "url": item.get("url") or "",
            "description": desc,
            "experience_level": parse_experience_level(title, desc),
            "posted_at": item.get("date"),
        })
    logger.info("Careerjet: %d jobs for '%s' / '%s'", len(jobs), query, location)
    return jobs
