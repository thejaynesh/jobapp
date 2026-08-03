import logging
from concurrent.futures import ThreadPoolExecutor

import httpx

from app.services.sources.base import (
    board_workers,
    fetch_boards_concurrently,
    parse_experience_level,
)

logger = logging.getLogger(__name__)

_LIST_API = "https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100"
_DETAIL_API = "https://api.smartrecruiters.com/v1/companies/{slug}/postings/{posting_id}"
_PUBLIC_URL = "https://jobs.smartrecruiters.com/{slug}/{posting_id}"

# The postings list has no descriptions; each needs a detail call. Cap per company
# (the slug list can now carry dozens of companies per cycle).
_MAX_DETAIL_FETCHES = 25
_DETAIL_WORKERS = 5


def _fetch_description(slug: str, posting_id: str) -> str:
    try:
        resp = httpx.get(_DETAIL_API.format(slug=slug, posting_id=posting_id), timeout=15)
        resp.raise_for_status()
        sections = (resp.json().get("jobAd") or {}).get("sections") or {}
    except Exception as exc:
        logger.warning("SmartRecruiters detail error (%s/%s): %s", slug, posting_id, exc)
        return ""
    parts = []
    for section in sections.values():
        if isinstance(section, dict) and section.get("text"):
            parts.append(section["text"])
    return "\n\n".join(parts)


def fetch(company_slugs: list[str]) -> list[dict]:
    """Fetch jobs from SmartRecruiters' public postings API (no key required)."""

    def _fetch_one(slug: str) -> list[dict]:
        resp = httpx.get(_LIST_API.format(slug=slug), timeout=15)
        resp.raise_for_status()
        data = resp.json()

        items = data.get("content", [])
        # Descriptions gate the downstream skill filter, so fetch the capped
        # batch of them in parallel rather than serially per posting.
        detail_ids = [
            str(item.get("id", "")) for item in items[:_MAX_DETAIL_FETCHES]
            if item.get("id")
        ]
        descriptions: dict[str, str] = {}
        if detail_ids:
            with ThreadPoolExecutor(max_workers=min(_DETAIL_WORKERS, len(detail_ids))) as pool:
                descriptions = dict(
                    zip(detail_ids, pool.map(lambda pid: _fetch_description(slug, pid), detail_ids))
                )

        jobs = []
        for item in items:
            posting_id = str(item.get("id", ""))
            title = (item.get("name") or "").strip()
            loc = item.get("location") or {}
            location_parts = [loc.get("city"), loc.get("region"), loc.get("country")]
            location = ", ".join(p for p in location_parts if p)
            is_remote = bool(loc.get("remote")) or "remote" in title.lower()
            description = descriptions.get(posting_id, "")

            jobs.append({
                "source": "smartrecruiters",
                "source_job_id": posting_id,
                "title": title,
                "company": ((item.get("company") or {}).get("name") or slug).strip(),
                "location": location,
                "is_remote": is_remote,
                "url": _PUBLIC_URL.format(slug=slug, posting_id=posting_id),
                "description": description,
                "experience_level": parse_experience_level(title, description),
                "posted_at": item.get("releasedDate"),
            })
        return jobs

    return fetch_boards_concurrently(
        company_slugs, _fetch_one, "SmartRecruiters", board_workers()
    )
