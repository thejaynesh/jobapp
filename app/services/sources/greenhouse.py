"""
Greenhouse job boards: `boards-api.greenhouse.io/v1/boards/<slug>/jobs`.

A board lists every opening in one response, with or without the posting
text. With it (`?content=true`) is about twelve times the bytes: 99.3 MB
against 8.4 MB for 35 boards and 7,695 postings, 11.8 KB of text each
(measured 2026-09-28). Every cycle downloaded the text of every posting
again, nearly all of it already stored.

So the list is read without it, and text is fetched only for postings not
already held with a description (`base.described`, loaded by the fetcher):
one request each (`/jobs/<id>`) when there are a few, or the board once with
content when most of it is new — a first read, or a board that turned over.
Every posting still arrives whole; what stops is downloading the same text
every few hours. "Greenhouse descriptions on demand" on the settings page
turns this off.
"""

import logging
from datetime import datetime

import httpx

from app.services.sources.base import (
    age_cutoff,
    board_workers,
    cycle_cfg,
    described,
    fetch_boards_concurrently,
    parse_experience_level,
    saw_postings,
)

logger = logging.getLogger(__name__)

_BOARD = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
_POSTING = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}"
_TIMEOUT = 15
# Past this many new postings on one board (or a quarter of it), one read of
# the whole board with content beats a request per posting.
_PER_POSTING_LIMIT = 20


def _board(slug: str, content: bool) -> list[dict]:
    resp = httpx.get(_BOARD.format(slug=slug), params={"content": "true"} if content else None,
                     timeout=_TIMEOUT)
    resp.raise_for_status()
    return [item for item in (resp.json() or {}).get("jobs", []) if isinstance(item, dict)]


def _posting_content(slug: str, job_id) -> str:
    resp = httpx.get(_POSTING.format(slug=slug, job_id=job_id), timeout=_TIMEOUT)
    resp.raise_for_status()
    return (resp.json() or {}).get("content") or ""


def _as_job(slug: str, item: dict, desc: str) -> dict:
    title = item.get("title", "")
    loc = (item.get("location") or {}).get("name", "")
    return {
        "source": "greenhouse",
        "source_job_id": str(item.get("id", "")),
        "title": title,
        "company": slug,
        "location": loc,
        "is_remote": "remote" in loc.lower() or "remote" in title.lower(),
        "url": item.get("absolute_url", ""),
        # Empty for a posting already stored with its text: the save finds the
        # stored row, and a merge never replaces text with less.
        "description": desc,
        "experience_level": parse_experience_level(title, desc),
        # `first_published` is when the posting went up; `updated_at` moves
        # on every edit, so an old requisition someone re-saved looked new
        # and a posting's age read as the age of its last typo fix.
        "posted_at": item.get("first_published") or item.get("updated_at") or None,
    }


def fetch(company_slugs: list[str], max_age_days=None) -> list[dict]:
    cutoff = age_cutoff(max_age_days)
    on_demand = bool(getattr(cycle_cfg(), "GREENHOUSE_DESCRIPTIONS_ON_DEMAND", True))
    known = described("greenhouse") if on_demand else frozenset()

    def fresh(item: dict) -> bool:
        dated_raw = item.get("first_published") or item.get("updated_at") or ""
        if dated_raw and cutoff is not None:
            try:
                return datetime.fromisoformat(dated_raw.replace("Z", "+00:00")) >= cutoff
            except Exception:
                return True
        return True

    def _fetch_one(slug: str) -> list[dict]:
        items = _board(slug, content=not on_demand)
        saw_postings(str(item.get("id", "")) for item in items)
        keep = [item for item in items if fresh(item)]
        if not on_demand:
            return [_as_job(slug, item, item.get("content", "")) for item in keep]

        new = [item for item in keep if str(item.get("id", "")) not in known]
        texts: dict[str, str] = {}
        if len(new) > max(_PER_POSTING_LIMIT, len(keep) // 4):
            texts = {str(item.get("id", "")): item.get("content") or ""
                     for item in _board(slug, content=True)}
        else:
            for item in new:
                try:
                    texts[str(item.get("id", ""))] = _posting_content(slug, item.get("id"))
                except Exception as exc:
                    # Stored without its text; enrichment reads Greenhouse's
                    # posting API for it later.
                    logger.warning("Greenhouse posting %s/%s: %s", slug, item.get("id"), exc)
        return [_as_job(slug, item, texts.get(str(item.get("id", "")), "")) for item in keep]

    return fetch_boards_concurrently(
        company_slugs, _fetch_one, "Greenhouse", board_workers()
    )
