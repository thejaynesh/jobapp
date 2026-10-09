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
    BoardResult,
    board_cursor,
    board_workers,
    cycle_cfg,
    fetch_boards_concurrently,
    parse_experience_level,
    rank_by_title,
    saw_postings,
)

logger = logging.getLogger(__name__)

_API = "https://ats.rippling.com/api/v2/board/{slug}/jobs"
_PAGE_SIZE = 100
_TIMEOUT = 20


def list_page(slug: str, page: int = 0) -> tuple[list[dict], int | None]:
    resp = httpx.get(_API.format(slug=slug), params={"page": page, "pageSize": _PAGE_SIZE},
                     timeout=_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError("Rippling listing response has no items list")
    if any(not isinstance(item, dict) for item in data["items"]):
        raise ValueError("Rippling listing contains an unreadable posting")
    raw_pages = data.get("totalPages")
    if raw_pages is None:
        pages = None
    elif isinstance(raw_pages, bool) or not str(raw_pages).strip().isdigit():
        raise ValueError("Rippling listing has an invalid totalPages value")
    else:
        pages = int(raw_pages)
    return data["items"], pages


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
    cfg = cycle_cfg()
    page_limit = max(1, int(cfg.RIPPLING_MAX_PAGES))
    detail_limit = max(0, int(cfg.RIPPLING_DETAIL_LIMIT))

    def _fetch_one(slug: str) -> BoardResult:
        rows: list[dict] = []
        seen = set()
        try:
            initial = max(0, int(board_cursor("rippling", slug).get("page", 0)))
        except (TypeError, ValueError):
            initial = 0
        page = initial
        finished = False
        restart = False
        error = category = None
        for _ in range(page_limit):
            try:
                items, pages = list_page(slug, page)
            except Exception as exc:
                # A later request must not discard postings from earlier pages,
                # or reset a resumed board to page zero when that request fails.
                error = str(exc) or type(exc).__name__
                status = getattr(getattr(exc, "response", None), "status_code", None)
                category = ("rate_limited" if status == 429 else "not_found" if status in (404, 410)
                            else "unauthorized" if status in (401, 403)
                            else "pagination" if isinstance(exc, ValueError) else "request_failed")
                if status in (404, 410) and page > 0:
                    # A board can shrink while a later page is queued. Only
                    # page zero can prove the board itself disappeared.
                    error = f"Rippling page {page} no longer exists; restarting from page zero: {error}"
                    category = "pagination"
                    page, restart = 0, True
                break
            new = []
            malformed = False
            for row in items:
                identifier = str(row.get("id") or "")
                if not identifier or not isinstance(row.get("name"), str) or not row["name"].strip():
                    malformed = True
                    continue
                if identifier not in seen:
                    seen.add(identifier)
                    new.append(row)
            rows.extend(new)
            if malformed:
                error, category = "Rippling listing contains a posting without an id or title", "pagination"
                break
            if items and not new:
                error, category = "Rippling pagination repeated a page; retry needs verification", "pagination"
                break
            if not items and pages is not None and page + 1 < pages:
                error, category = "Rippling returned an empty page before its reported final page", "pagination"
                break
            if items and pages is not None and page >= pages:
                error, category = "Rippling returned postings beyond its reported page count", "pagination"
                break
            page += 1
            # Without page metadata, confirm an empty page instead of assuming
            # the first (possibly capped) response contains the whole board.
            if not items or (pages is not None and page >= pages):
                finished = True
                break
        described = {
            r.get("id") for r in rank_by_title(rows, queries, lambda r: r.get("name"))
            [:detail_limit]
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
                "_listed_open": True,
            })
        # A resumed tail is useful collection work, but cannot prove earlier
        # postings disappeared. Only one full traversal can close missing jobs.
        complete = initial == 0 and finished and error is None
        if complete:
            saw_postings(seen)
        return BoardResult(jobs=jobs, complete=complete,
                           total=len(seen) if complete else None,
                           cursor={"page": page} if not finished and (rows or page or restart) else None,
                           error=error, error_category=category)

    return fetch_boards_concurrently(company_slugs, _fetch_one, "Rippling", board_workers())
