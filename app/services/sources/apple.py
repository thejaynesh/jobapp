"""
Apple's own careers search, read by the server.

Apple lists 4,207 US postings for "software engineer" (measured 2026-09-28)
and none of them reached us except second-hand. Its search page is rendered
on the server with the results embedded as the page's own hydration data —
there is no separate API to call, and no robots.txt:

    GET https://jobs.apple.com/en-us/search?search=software+engineer
        &sort=newest&location=united-states-USA&page=1

20 postings a page, newest first, each with its posting date and location but
only a summary, which is mostly Apple's standard introduction. The detail page
carries the rest — description, responsibilities, minimum and preferred
qualifications — the same way, so the postings whose titles match the
profile's roles get one more request each, up to a budget
(`base.rank_by_title`, as the Workday adapter spends its own). A posting
whose detail this process has already read is not read again for a week: the
stored description is the longer one and a later summary never replaces it
(`deduplication.merge_description`), so the budget goes to new postings rather
than to re-reading the same ones every cycle. Whatever the budget leaves,
enrichment reads from the same page later (`enrichment._apple`).

Location filters are Apple's own slugs, not ISO codes (Canada is `CANC`), and
an unknown one silently returns US postings, so only slugs read back live are
mapped and every result is checked against the country asked for.
"""

import json
import logging
import re
import time

import httpx

from app.services.descriptions import clean as clean_description
from app.services.sources.base import parse_experience_level, raise_if_blocked, rank_by_title

logger = logging.getLogger(__name__)

_BASE = "https://jobs.apple.com/en-us"
_PAGE_SIZE = 20
DEFAULT_MAX_PAGES = 3
DEFAULT_MAX_DETAILS = 20
_TIMEOUT = 30
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}

# The two-letter codes the rest of the fetcher uses, as Apple's location slug
# and the country name its results carry. Each was read back live.
_COUNTRIES = {
    "us": ("united-states-USA", "United States of America"),
    "ca": ("canada-CANC", "Canada"),
    "gb": ("united-kingdom-GBR", "United Kingdom"),
    "de": ("germany-DEU", "Germany"),
    "ie": ("ireland-IRL", "Ireland"),
    "sg": ("singapore-SGP", "Singapore"),
    "pl": ("poland-POL", "Poland"),
}

# positionId → when this process last read its detail page.
_DETAILED: dict[str, float] = {}
_DETAIL_MEMORY_SECONDS = 7 * 24 * 3600
_DETAIL_MEMORY_SIZE = 20_000

_HYDRATION = re.compile(r"__staticRouterHydrationData\s*=\s*JSON\.parse\((\".*?\")\);", re.S)
_INTERN = re.compile(r"\bintern(ship)?s?\b", re.I)


def countries_for(codes: list[str] | None) -> list[str]:
    """Apple's location slugs for the profile's countries; the US when none."""
    mapped = [_COUNTRIES[c.lower()][0] for c in (codes or []) if c.lower() in _COUNTRIES]
    return list(dict.fromkeys(mapped)) or [_COUNTRIES["us"][0]]


def _country_name(slug: str) -> str | None:
    return next((name for s, name in _COUNTRIES.values() if s == slug), None)


def loader_data(page: str) -> dict:
    """The hydration data a page embeds for its own client."""
    match = _HYDRATION.search(page or "")
    if not match:
        raise ValueError("Apple page carried no hydration data")
    return (json.loads(json.loads(match.group(1))) or {}).get("loaderData") or {}


def _get(url: str, params: dict | None = None) -> str:
    resp = httpx.get(url, params=params, headers=_HEADERS, timeout=_TIMEOUT,
                     follow_redirects=True)
    raise_if_blocked(resp, "Apple")
    resp.raise_for_status()
    return resp.text


def search(query: str, location: str, page: int = 1) -> tuple[list[dict], int]:
    """One page of a search, newest first, and the total it reports."""
    data = loader_data(_get(f"{_BASE}/search", {
        "search": query, "sort": "newest", "location": location, "page": page,
    })).get("search") or {}
    rows = [r for r in (data.get("searchResults") or []) if isinstance(r, dict)]
    try:
        total = int(data.get("totalRecords") or 0)
    except (TypeError, ValueError):
        total = 0
    return rows, total


def detail(position_id: str, slug: str) -> dict:
    """The full posting behind a search result."""
    data = loader_data(_get(f"{_BASE}/details/{position_id}/{slug}"))
    return (data.get("jobDetails") or {}).get("jobsData") or {}


def _url(row: dict) -> str:
    return f"{_BASE}/details/{row.get('positionId')}/{row.get('transformedPostingTitle') or ''}".rstrip("/")


def _place(loc: dict) -> str:
    parts = [loc.get("city") or loc.get("name"), loc.get("stateProvince"), loc.get("countryName")]
    parts = ["United States" if p == "United States of America" else p for p in parts if p]
    return ", ".join(dict.fromkeys(parts))


def _location(locations) -> str:
    """Every place a position is posted to, in the order Apple lists them."""
    places = [_place(loc) for loc in (locations or []) if isinstance(loc, dict)]
    return "; ".join(dict.fromkeys(p for p in places if p))


def full_description(data: dict) -> str:
    parts = [data.get("jobSummary") or ""]
    for label, key in (("Description", "description"),
                       ("Responsibilities", "responsibilities"),
                       ("Minimum Qualifications", "minimumQualifications"),
                       ("Preferred Qualifications", "preferredQualifications")):
        text = data.get(key)
        if isinstance(text, str) and text.strip():
            parts.append(f"{label}\n{text}")
    return clean_description("\n\n".join(p for p in parts if p))


def _as_job(row: dict, extra: dict | None = None) -> dict | None:
    position_id = str(row.get("positionId") or "").strip()
    title = (row.get("postingTitle") or "").strip()
    if not position_id or not title:
        return None
    extra = extra or {}
    description = full_description(extra) if extra else clean_description(row.get("jobSummary") or "")
    location = _location(extra.get("locations") if isinstance(extra.get("locations"), list)
                         else row.get("locations"))
    intern = bool(_INTERN.search(title))
    return {
        "source": "apple",
        "source_job_id": position_id,
        "title": title,
        "company": "Apple",
        "location": location,
        "is_remote": bool(row.get("homeOffice")),
        "url": _url(row),
        "description": description,
        "experience_level": parse_experience_level(title, description),
        "posted_at": row.get("postDateInGMT"),
        **({"employment_type": "internship"} if intern else {}),
    }


def _remember(position_id: str, when: float) -> None:
    if len(_DETAILED) >= _DETAIL_MEMORY_SIZE:
        for stale in sorted(_DETAILED, key=_DETAILED.get)[: _DETAIL_MEMORY_SIZE // 10]:
            del _DETAILED[stale]
    _DETAILED[position_id] = when


def fetch(query: str, location: str = _COUNTRIES["us"][0], max_pages: int | None = None,
          max_details: int | None = None) -> list[dict]:
    """Up to `max_pages` pages of one search, newest first; the titles matching
    `query` best get their full description, up to `max_details` of them."""
    pages = DEFAULT_MAX_PAGES if max_pages is None else max(1, int(max_pages))
    budget = DEFAULT_MAX_DETAILS if max_details is None else max(0, int(max_details))
    country = _country_name(location)
    rows: dict[str, dict] = {}
    for page in range(1, pages + 1):
        try:
            found, total = search(query, location, page)
        except Exception as exc:
            if page == 1:
                raise
            logger.warning("Apple: page %d of %r failed: %s", page, query, exc)
            break
        for row in found:
            countries = {loc.get("countryName") for loc in row.get("locations") or []
                         if isinstance(loc, dict)}
            if country and countries and country not in countries:
                continue
            key = str(row.get("positionId") or "")
            if not key:
                continue
            # One position posted to several places is one row per place.
            if key in rows:
                rows[key]["locations"] = [*rows[key].get("locations", []),
                                          *(row.get("locations") or [])]
            else:
                rows[key] = {**row, "locations": list(row.get("locations") or [])}
        if len(found) < _PAGE_SIZE or page * _PAGE_SIZE >= total:
            break

    ordered = rank_by_title(list(rows.values()), [query], lambda r: r.get("postingTitle"))
    now = time.monotonic()
    jobs = []
    for row in ordered:
        extra = {}
        key = str(row["positionId"])
        read_at = _DETAILED.get(key)
        if budget > 0 and (read_at is None or now - read_at > _DETAIL_MEMORY_SECONDS):
            budget -= 1
            try:
                extra = detail(key, row.get("transformedPostingTitle") or "")
                _remember(key, now)
            except Exception as exc:
                logger.warning("Apple detail error (%s): %s", key, exc)
        job = _as_job(row, extra)
        if job:
            jobs.append(job)
    logger.info("Apple: %d jobs for %r in %s", len(jobs), query, location)
    return jobs
