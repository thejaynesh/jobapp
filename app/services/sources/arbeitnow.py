"""
Arbeitnow's free public job-board feed (primarily remote/EU tech roles).

Two things were wrong here and they had the same root cause. The endpoint was
requested without `www.`, which 301-redirects, and httpx doesn't follow
redirects by default — so every call failed. Then, because this is a *feed*
rather than a search API, the adapter was downloading the entire board once per
(query, location) pair and filtering locally: with expanded queries that's
dozens of identical requests per cycle, which earned a 429.

The feed is now fetched once per cycle and cached, then filtered per query in
memory. Same results, one request instead of thirty.
"""

import logging
import time

import httpx

from app.services.sources.base import (
    SourceUnavailable,
    parse_experience_level,
    raise_if_blocked,
)

logger = logging.getLogger(__name__)

# `www.` matters: the bare host 301s and the redirect used to go unfollowed.
_BASE = "https://www.arbeitnow.com/api/job-board-api"

_HEADERS = {"User-Agent": "jobapp/1.0 (+https://github.com/thejaynesh/jobapp)"}

DEFAULT_MAX_PAGES = 3
# Long enough that one fetch cycle reuses a single download, short enough that
# the next cycle sees fresh postings.
_CACHE_TTL_SECONDS = 900

_cache: dict = {"at": 0.0, "items": []}


def _feed(max_pages: int) -> list[dict]:
    """The board feed, downloaded at most once per cache window."""
    now = time.monotonic()
    if _cache["items"] and (now - _cache["at"]) < _CACHE_TTL_SECONDS:
        return _cache["items"]

    items: list[dict] = []
    with httpx.Client(timeout=15, follow_redirects=True, headers=_HEADERS) as client:
        for page in range(1, max(1, max_pages) + 1):
            resp = client.get(_BASE, params={"page": page})
            raise_if_blocked(resp, "Arbeitnow")
            resp.raise_for_status()
            batch = resp.json().get("data") or []
            if not batch:
                break
            items.extend(batch)

    _cache["at"] = now
    _cache["items"] = items
    logger.info("Arbeitnow: cached %d feed entries from %d page(s)", len(items), max_pages)
    return items


def _matches(query: str, title: str, tags_text: str) -> bool:
    q_words = set(query.lower().split())
    if not q_words:
        return True
    searchable = f"{title} {tags_text}".lower()
    return bool(q_words.intersection(searchable.split())) or any(
        w in searchable for w in q_words
    )


def fetch(query: str, location: str, max_pages: int = DEFAULT_MAX_PAGES) -> list[dict]:
    """Jobs from the cached Arbeitnow feed matching `query`."""
    try:
        items = _feed(max_pages)
    except SourceUnavailable:
        raise  # rate limited — stop asking for the rest of the cycle
    except Exception as exc:
        logger.error("Arbeitnow fetch error: %s", exc)
        return []

    jobs: list[dict] = []
    for item in items:
        title = (item.get("title") or "").strip()
        tags_text = " ".join(item.get("tags") or [])
        if not _matches(query, title, tags_text):
            continue

        desc = item.get("description") or ""
        jobs.append({
            "source": "arbeitnow",
            "source_job_id": item.get("slug"),
            "title": title,
            "company": (item.get("company_name") or "").strip(),
            "location": (item.get("location") or location).strip(),
            "is_remote": bool(item.get("remote", False)),
            "url": item.get("url") or "",
            "description": desc,
            "experience_level": parse_experience_level(title, desc),
            "posted_at": item.get("created_at"),
        })

    logger.info("Arbeitnow: %d jobs for query '%s'", len(jobs), query)
    return jobs


def reset_cache() -> None:
    """Drop the cached feed (used by tests)."""
    _cache["at"] = 0.0
    _cache["items"] = []
