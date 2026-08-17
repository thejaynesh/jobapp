import logging

import httpx

from app.services.descriptions import clean as clean_description
from app.services.descriptions import looks_like_block_page
from app.services.sources.base import parse_experience_level

logger = logging.getLogger(__name__)

_BASE = "https://remoteok.com/api"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def fetch(query: str) -> list[dict]:
    """Fetch remote tech jobs from RemoteOK's free public API."""
    try:
        resp = httpx.get(_BASE, headers=_HEADERS, timeout=15, follow_redirects=True)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.error("RemoteOK fetch error: %s", exc)
        return []

    q_words = set(query.lower().split())
    jobs: list[dict] = []
    blocked = 0

    for item in data:
        if not isinstance(item, dict) or "position" not in item:
            continue

        position = (item.get("position") or "").strip()
        tags_text = " ".join(item.get("tags") or []).lower()
        searchable = (position + " " + tags_text).lower()

        if q_words and not q_words.intersection(searchable.split()):
            if not any(w in searchable for w in q_words):
                continue

        # RemoteOK sits behind a bot wall that sometimes answers a description
        # request with its challenge page. Storing that is worse than storing
        # nothing — it reads as a real description to everything downstream —
        # and a row that only ever had a wall in it was never a listing.
        desc = clean_description(item.get("description") or "")
        if not desc and looks_like_block_page(item.get("description") or ""):
            blocked += 1
            continue

        jobs.append({
            "source": "remoteok",
            "source_job_id": str(item.get("id", "")),
            "title": position,
            "company": (item.get("company") or "").strip(),
            "location": (item.get("location") or "Remote").strip(),
            "is_remote": True,
            "url": item.get("url") or "",
            "description": desc,
            "experience_level": parse_experience_level(position, desc),
            "posted_at": item.get("date"),
        })

    if blocked:
        logger.warning(
            "RemoteOK: %d listings answered with a challenge page instead of a "
            "description and were dropped", blocked,
        )
    logger.info("RemoteOK: %d jobs for query '%s'", len(jobs), query)
    return jobs
