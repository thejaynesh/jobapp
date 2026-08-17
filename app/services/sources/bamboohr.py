"""
BambooHR careers boards.

Every BambooHR customer gets `<company>.bamboohr.com/careers`, and that page is
driven by a public JSON endpoint that needs no key. The list call gives titles
and locations; the description lives behind one call per posting, which is why
those are fetched concurrently and capped — a company with 400 openings should
not own the whole cycle.

Descriptions the cap skips are not lost: enrichment reaches the same detail
endpoint from the stored URL afterwards.
"""

import logging
from concurrent.futures import ThreadPoolExecutor

import httpx

from app.services.descriptions import clean
from app.services.sources.base import (
    LISTING_HEADERS,
    board_workers,
    fetch_boards_concurrently,
    parse_experience_level,
)

logger = logging.getLogger(__name__)

_LIST_API = "https://{slug}.bamboohr.com/careers/list"
_DETAIL_API = "https://{slug}.bamboohr.com/careers/{job_id}/detail"
_PUBLIC_URL = "https://{slug}.bamboohr.com/careers/{job_id}"

# Per company, per cycle. One request each, and the rest arrive via enrichment.
_MAX_DETAILS_PER_BOARD = 40
_DETAIL_WORKERS = 4


def _text(value) -> str:
    """A string out of a field that may be plain, or an object with a label."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        for key in ("label", "name", "text", "value"):
            if key in value:
                return _text(value[key])
    return ""


def _location(item: dict) -> str:
    """BambooHR splits the address across several optional fields."""
    for key in ("location", "atsLocation"):
        block = item.get(key)
        if isinstance(block, str) and block.strip():
            return block.strip()
        if isinstance(block, dict):
            parts = [
                _text(block.get(field))
                for field in ("city", "state", "country", "name", "label")
            ]
            joined = ", ".join(p for p in parts if p)
            if joined:
                return joined
    return _text(item.get("locationLabel"))


def _detail_description(slug: str, job_id: str) -> str:
    try:
        resp = httpx.get(
            _DETAIL_API.format(slug=slug, job_id=job_id),
            headers=LISTING_HEADERS, timeout=15, follow_redirects=True,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.debug("BambooHR detail fetch failed (%s/%s): %s", slug, job_id, exc)
        return ""

    if not isinstance(data, dict):
        return ""
    # The payload nests the posting under a wrapper whose name has moved
    # before; take whichever of them is present rather than one path.
    for key in ("jobOpeningShare", "result", "job", "data"):
        block = data.get(key)
        if isinstance(block, dict):
            data = block
            break
    return clean(
        data.get("description")
        or data.get("jobOpeningDescription")
        or data.get("descriptionHtml")
        or ""
    )


def fetch(company_slugs: list[str]) -> list[dict]:
    """Fetch jobs from BambooHR's public careers API (no key required)."""

    def _fetch_one(slug: str) -> list[dict]:
        resp = httpx.get(
            _LIST_API.format(slug=slug), headers=LISTING_HEADERS,
            timeout=15, follow_redirects=True,
        )
        resp.raise_for_status()
        data = resp.json()

        items = data.get("result") if isinstance(data, dict) else data
        if not isinstance(items, list):
            return []

        jobs = []
        for item in items:
            if not isinstance(item, dict):
                continue
            title = _text(item.get("jobOpeningName")) or _text(item.get("title"))
            job_id = _text(item.get("id")) or _text(item.get("jobOpeningId"))
            if not title or not job_id:
                continue
            location = _location(item)
            jobs.append({
                "source": "bamboohr",
                "source_job_id": job_id,
                "title": title,
                "company": _text(item.get("companyName")) or slug,
                "location": location,
                "is_remote": bool(item.get("isRemote"))
                or "remote" in f"{location} {title}".lower(),
                "url": _PUBLIC_URL.format(slug=slug, job_id=job_id),
                "description": "",
                "experience_level": parse_experience_level(title, ""),
                "posted_at": item.get("datePosted") or item.get("postedDate"),
            })

        _fill_descriptions(slug, jobs)
        return jobs

    return fetch_boards_concurrently(company_slugs, _fetch_one, "BambooHR", board_workers())


def _fill_descriptions(slug: str, jobs: list[dict]) -> None:
    """One detail call per posting, in parallel, up to the per-board cap."""
    targets = jobs[:_MAX_DETAILS_PER_BOARD]
    if not targets:
        return

    def _one(job: dict) -> None:
        description = _detail_description(slug, job["source_job_id"])
        if description:
            job["description"] = description
            job["experience_level"] = parse_experience_level(job["title"], description)

    with ThreadPoolExecutor(
        max_workers=max(1, min(_DETAIL_WORKERS, len(targets)))
    ) as pool:
        list(pool.map(_one, targets))
