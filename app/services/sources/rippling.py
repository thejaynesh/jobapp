"""
Rippling ATS boards (`ats.rippling.com/<slug>/jobs`).

Rippling's recruiting product hosts a growing number of startups' boards.
Each board's own page reads a public JSON API (checked on
ats.rippling.com/flexai, 2026-09-28):

    GET https://ats.rippling.com/api/v2/board/{slug}/jobs?page=0&pageSize=100

The listing has title, URL and locations with their workplace type; the
description is one request per posting (`…/jobs/{id}`), spent on the titles
matching wants.
"""

import logging

import httpx

from app.services.descriptions import clean as clean_description
from app.services.sources.base import (
    board_workers,
    fetch_boards_concurrently,
    parse_experience_level,
    rank_by_title,
)

logger = logging.getLogger(__name__)

_API = "https://ats.rippling.com/api/v2/board/{slug}/jobs"
_PAGE_SIZE = 100
_MAX_PAGES = 5
_MAX_DETAILS = 15
_TIMEOUT = 20


def list_page(slug: str, page: int = 0) -> tuple[list[dict], int]:
    resp = httpx.get(_API.format(slug=slug), params={"page": page, "pageSize": _PAGE_SIZE},
                     timeout=_TIMEOUT)
    resp.raise_for_status()
    data = resp.json() or {}
    try:
        pages = int(data.get("totalPages") or 1)
    except (TypeError, ValueError):
        pages = 1
    return [i for i in (data.get("items") or []) if isinstance(i, dict)], pages


def detail(slug: str, job_id: str) -> str:
    """The posting's description sections, joined; "" if unreadable."""
    try:
        resp = httpx.get(f"{_API.format(slug=slug)}/{job_id}", timeout=_TIMEOUT)
        resp.raise_for_status()
        sections = (resp.json() or {}).get("description") or {}
    except Exception as exc:
        logger.warning("Rippling detail error (%s %s): %s", slug, job_id, exc)
        return ""
    if isinstance(sections, str):
        return clean_description(sections)
    return clean_description("\n\n".join(
        str(v) for v in (sections.values() if isinstance(sections, dict) else [])
        if isinstance(v, str) and v.strip()))


def fetch(company_slugs: list[str], queries: list[str] | None = None) -> list[dict]:
    queries = list(queries or [])

    def _fetch_one(slug: str) -> list[dict]:
        rows: list[dict] = []
        page, pages = 0, 1
        while page < min(pages, _MAX_PAGES):
            items, pages = list_page(slug, page)
            rows.extend(items)
            page += 1
            if not items:
                break
        described = {
            r.get("id") for r in rank_by_title(rows, queries, lambda r: r.get("name"))
            [:_MAX_DETAILS]
        }
        jobs = []
        for row in rows:
            job_id, title = str(row.get("id") or ""), (row.get("name") or "").strip()
            if not job_id or not title:
                continue
            places = [loc for loc in (row.get("locations") or []) if isinstance(loc, dict)]
            location = "; ".join(p.get("name") or "" for p in places if p.get("name"))
            remote = any(str(p.get("workplaceType") or "").upper() == "REMOTE" for p in places)
            description = detail(slug, job_id) if row.get("id") in described else ""
            jobs.append({
                "source": "rippling",
                "source_job_id": job_id,
                "title": title,
                # The board's slug; the registry's name for the board replaces
                # it (`job_fetcher._name_board_jobs`).
                "company": slug,
                "location": location,
                "is_remote": remote or "remote" in location.lower(),
                "url": row.get("url") or f"https://ats.rippling.com/{slug}/jobs/{job_id}",
                "description": description,
                "experience_level": parse_experience_level(title, description),
                "posted_at": None,
            })
        return jobs

    return fetch_boards_concurrently(company_slugs, _fetch_one, "Rippling", board_workers())
