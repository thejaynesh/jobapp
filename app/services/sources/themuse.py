import logging

import httpx

from app.services.sources.base import parse_experience_level

logger = logging.getLogger(__name__)

_BASE = "https://www.themuse.com/api/public/jobs"
_CATEGORIES = ["Software Engineering", "Data and Analytics"]
_PAGES = 2

# The Muse tags jobs with level objects; map their short names onto ours.
_LEVEL_MAP = {
    "internship": "entry",
    "entry": "entry",
    "mid": "mid",
    "senior": "senior",
    "management": "senior",
}


def _parse_level(item: dict, title: str, desc: str) -> str:
    for level in item.get("levels") or []:
        short = (level.get("short_name") or "").lower()
        if short in _LEVEL_MAP:
            return _LEVEL_MAP[short]
    return parse_experience_level(title, desc)


def fetch(query: str) -> list[dict]:
    """Fetch jobs from The Muse's free public API (no key required)."""
    jobs: list[dict] = []
    seen: set[str] = set()
    q_words = set(query.lower().split())

    for category in _CATEGORIES:
        for page in range(1, _PAGES + 1):
            try:
                resp = httpx.get(
                    _BASE,
                    params={"category": category, "page": page},
                    timeout=15,
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                logger.warning("The Muse fetch error (%s p%d): %s", category, page, exc)
                break

            for item in data.get("results", []):
                job_id = str(item.get("id", ""))
                if not job_id or job_id in seen:
                    continue

                title = (item.get("name") or "").strip()
                searchable = title.lower()
                if q_words and not any(w in searchable for w in q_words):
                    continue

                seen.add(job_id)
                desc = item.get("contents") or ""
                locations = [
                    (loc.get("name") or "").strip()
                    for loc in (item.get("locations") or [])
                ]
                location = "; ".join(l for l in locations if l)
                is_remote = any("remote" in l.lower() or "flexible" in l.lower() for l in locations)

                jobs.append({
                    "source": "themuse",
                    "source_job_id": job_id,
                    "title": title,
                    "company": ((item.get("company") or {}).get("name") or "").strip(),
                    "location": location,
                    "is_remote": is_remote,
                    "url": (item.get("refs") or {}).get("landing_page") or "",
                    "description": desc,
                    "experience_level": _parse_level(item, title, desc),
                    "posted_at": item.get("publication_date"),
                })

            # Stop paging early when the API says there are no more pages.
            if page >= int(data.get("page_count") or 1):
                break

    logger.info("The Muse: %d jobs for query '%s'", len(jobs), query)
    return jobs
