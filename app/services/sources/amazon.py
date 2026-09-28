"""
Amazon's own careers search, read by the server.

Amazon is one of the largest US employers of new graduates, and its postings
reached us only through the browser crawl of amazon.jobs — when a browser was
running — or second-hand through aggregators. Its search page reads a public
JSON endpoint, which `robots.txt` leaves open (only `/internal` is disallowed):

    GET https://www.amazon.jobs/en/search.json
        ?base_query=software+engineer&country=USA&result_limit=100&offset=0&sort=recent

100 a page, with the full description, basic and preferred qualifications,
posting date and location inline — 1,462 US matches for "software engineer",
measured 2026-09-28. No second request per posting.
"""

import logging
from datetime import datetime, timezone

import httpx

from app.services.descriptions import clean as clean_description
from app.services.sources.base import parse_experience_level, raise_if_blocked

logger = logging.getLogger(__name__)

_SEARCH = "https://www.amazon.jobs/en/search.json"
_BASE = "https://www.amazon.jobs"
_PAGE_SIZE = 100
DEFAULT_MAX_PAGES = 2
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

# The two-letter codes the rest of the fetcher uses for countries, as the
# three-letter ones Amazon filters on.
_ISO3 = {
    "us": "USA", "ca": "CAN", "gb": "GBR", "ie": "IRL", "de": "DEU", "fr": "FRA",
    "nl": "NLD", "es": "ESP", "it": "ITA", "pl": "POL", "in": "IND", "au": "AUS",
    "jp": "JPN", "sg": "SGP", "mx": "MEX", "br": "BRA",
}


def countries_for(codes: list[str] | None) -> list[str]:
    """Amazon country filters for the profile's countries; the US when none."""
    mapped = [_ISO3[c.lower()] for c in (codes or []) if c.lower() in _ISO3]
    return list(dict.fromkeys(mapped)) or ["USA"]


def _posted_at(text: str | None) -> str | None:
    """'September 25, 2026' → ISO date."""
    try:
        return datetime.strptime((text or "").strip(), "%B %d, %Y").replace(
            tzinfo=timezone.utc).isoformat()
    except ValueError:
        return None


def _description(item: dict) -> str:
    parts = [item.get("description") or ""]
    for label, key in (("Basic qualifications", "basic_qualifications"),
                       ("Preferred qualifications", "preferred_qualifications")):
        if item.get(key):
            parts.append(f"<h3>{label}</h3>{item[key]}")
    return clean_description("\n\n".join(p for p in parts if p))


def _as_job(item: dict) -> dict | None:
    title = (item.get("title") or "").strip()
    path = (item.get("job_path") or "").strip()
    if not title or not path:
        return None
    description = _description(item)
    location = (item.get("normalized_location") or item.get("location") or "").strip()
    return {
        "source": "amazon",
        "source_job_id": str(item.get("id_icims") or item.get("id") or "") or None,
        "title": title,
        # One name for every Amazon entity ("Amazon.com Services LLC", "Amazon
        # Web Services, Inc.") so the same opening from an aggregator dedupes.
        "company": "Amazon",
        "location": location,
        "is_remote": any(w in location.lower() for w in ("virtual", "remote")),
        "url": f"{_BASE}{path}",
        "description": description,
        "experience_level": parse_experience_level(title, description),
        "posted_at": _posted_at(item.get("posted_date")),
        **({"employment_type": "internship"} if item.get("is_intern") else {}),
    }


def fetch(query: str, country: str = "USA", max_pages: int | None = None) -> list[dict]:
    """Up to `max_pages` pages of one search, newest first."""
    pages = DEFAULT_MAX_PAGES if max_pages is None else max(1, int(max_pages))
    jobs: dict[str, dict] = {}
    for page in range(pages):
        params = {"base_query": query, "country": country, "result_limit": _PAGE_SIZE,
                  "offset": page * _PAGE_SIZE, "sort": "recent"}
        resp = httpx.get(_SEARCH, params=params, headers=_HEADERS, timeout=30)
        raise_if_blocked(resp, "Amazon")
        resp.raise_for_status()
        data = resp.json() or {}
        rows = data.get("jobs") or []
        for item in rows:
            job = _as_job(item) if isinstance(item, dict) else None
            if job:
                jobs.setdefault(job["source_job_id"] or job["url"], job)
        try:
            hits = int(data.get("hits") or 0)
        except (TypeError, ValueError):
            hits = 0
        if len(rows) < _PAGE_SIZE or (page + 1) * _PAGE_SIZE >= hits:
            break
    logger.info("Amazon: %d jobs for %r in %s", len(jobs), query, country)
    return list(jobs.values())
