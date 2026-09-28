"""
Oracle Taleo career sections: `<tenant>.taleo.net/careersection/<section>/`.

Taleo still carries large US employers — Textron, Cincinnati Financial, Mass
General, West Virginia University — and 160 rows of SimplifyJobs' lists.
A career section's search page calls a JSON endpoint of its own, with no key,
once it knows the section's portal number, which the search page states:

    GET  https://textron.taleo.net/careersection/textron/jobsearch.ftl?lang=en
         → queryString: 'lang=en&portal=8140753014'
    POST https://textron.taleo.net/careersection/rest/jobboard/searchjobs
         ?lang=en&portal=8140753014   {"fieldData": {"fields": {"KEYWORD": …}}, "pageNo": 1, …}

25 a page (measured 2026-09-28). Each section chooses its own columns, so
the title is the one the row says is linked, locations are the ones it says
are locations, and a date is whichever column reads as one. No
description: the detail page is 400 KB with the text URL-encoded in a hidden
field, so it is left to enrichment (`enrichment._taleo`), which reads it once
per posting rather than once per cycle.

Older sections have no portal number (Kaiser's `kp/external`), and the
validation probe keeps those out of the registry.

A board is `tenant/section`: `textron/textron`, `cinfin/ex`.
"""

import json
import logging
import re
from datetime import datetime, timezone

import httpx

from app.services.sources.base import board_workers, fetch_boards_concurrently, parse_experience_level

logger = logging.getLogger(__name__)

_PAGE_SIZE = 25
_MAX_QUERIES = 5
_MAX_PAGES_PER_QUERY = 4
_TIMEOUT = 30
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    # Taleo's gzip is malformed on some responses; plain is always fine.
    "Accept-Encoding": "identity",
}
_SPEC = re.compile(r"^([a-z0-9-]+)/([A-Za-z0-9_]+)$")
_PORTAL = re.compile(r"portal=(\d+)")
_DATE = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")

_FILTERS = ("POSTING_DATE", "LOCATION", "JOB_FIELD")
_ADVANCED = ("ORGANIZATION", "LOCATION", "JOB_FIELD", "JOB_NUMBER", "URGENT_JOB",
             "EMPLOYEE_STATUS", "STUDY_LEVEL", "WILL_TRAVEL", "JOB_SHIFT")


def parse_spec(spec: str) -> tuple[str, str] | None:
    match = _SPEC.match((spec or "").strip())
    if not match:
        logger.warning("Taleo: invalid board spec %r (want tenant/section)", spec)
        return None
    return match.group(1).lower(), match.group(2)


def _base(tenant: str) -> str:
    return f"https://{tenant}.taleo.net/careersection"


def posting_url(tenant: str, section: str, contest_no: str) -> str:
    return f"{_base(tenant)}/{section}/jobdetail.ftl?job={contest_no}&lang=en"


def portal(tenant: str, section: str) -> str | None:
    """The section's portal number, from its own search page; None when it has none."""
    resp = httpx.get(f"{_base(tenant)}/{section}/jobsearch.ftl", params={"lang": "en"},
                     headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True)
    resp.raise_for_status()
    match = _PORTAL.search(resp.text)
    return match.group(1) if match else None


def search(tenant: str, section: str, portal_no: str, keyword: str,
           page: int = 1) -> tuple[list[dict], int]:
    """One page of a keyword search, newest first, and the total it reports."""
    body = {
        "multilineEnabled": False,
        "sortingSelection": {"sortBySelectionParam": "3", "ascendingSortingOrder": "false"},
        "fieldData": {"fields": {"KEYWORD": keyword, "LOCATION": "", "CATEGORY": ""},
                      "valid": True},
        "filterSelectionParam": {"searchFilterSelections": [
            {"id": f, "selectedValues": []} for f in _FILTERS]},
        "advancedSearchFiltersSelectionParam": {"searchFilterSelections": [
            {"id": f, "selectedValues": []} for f in _ADVANCED]},
        "pageNo": page,
    }
    resp = httpx.post(
        f"{_base(tenant)}/rest/jobboard/searchjobs",
        params={"lang": "en", "portal": portal_no}, json=body, timeout=_TIMEOUT,
        headers={**_HEADERS, "Content-Type": "application/json",
                 "Accept": "application/json", "tz": "GMT+00:00",
                 "Referer": f"{_base(tenant)}/{section}/jobsearch.ftl?lang=en"},
    )
    resp.raise_for_status()
    data = resp.json() or {}
    rows = [r for r in (data.get("requisitionList") or []) if isinstance(r, dict)]
    try:
        total = int((data.get("pagingData") or {}).get("totalCount") or 0)
    except (TypeError, ValueError):
        total = 0
    return rows, total


def _column(row: dict, index) -> str:
    columns = row.get("column") or []
    try:
        return str(columns[int(index)] or "").strip()
    except (TypeError, ValueError, IndexError):
        return ""


def _locations(row: dict) -> list[str]:
    places: list[str] = []
    for index in row.get("locationsColumns") or []:
        text = _column(row, index)
        try:
            value = json.loads(text) if text.startswith("[") else [text]
        except ValueError:
            value = [text]
        places += [str(v).strip() for v in value if str(v).strip()]
    return list(dict.fromkeys(places))


def _posted_at(row: dict) -> str | None:
    for text in row.get("column") or []:
        text = str(text or "").strip()
        if _DATE.match(text):
            try:
                return datetime.strptime(text, "%m/%d/%Y").replace(tzinfo=timezone.utc).isoformat()
            except ValueError:
                return None
    return None


def _as_job(row: dict, tenant: str, section: str, spec: str) -> dict | None:
    title = _column(row, row.get("linkedColumn", 0))
    contest = str(row.get("contestNo") or "").strip()
    if not title or not contest:
        return None
    location = "; ".join(_locations(row))
    return {
        "source": "taleo",
        "source_job_id": f"{tenant}:{contest}",
        "title": title,
        # The board spec: the registry swaps in the name the board is filed
        # under (`job_fetcher._name_board_jobs`), since Taleo pages give none.
        "company": spec,
        "location": location,
        "is_remote": "remote" in f"{title} {location}".lower(),
        "url": posting_url(tenant, section, contest),
        "description": "",
        "experience_level": parse_experience_level(title, ""),
        "posted_at": _posted_at(row),
    }


def fetch(company_slugs: list[str], queries: list[str] | None = None) -> list[dict]:
    """Each section searched for each role, newest first."""
    queries = [q for q in dict.fromkeys(queries or []) if q and q.strip()][:_MAX_QUERIES]

    def _fetch_one(spec: str) -> list[dict]:
        parsed = parse_spec(spec)
        if not parsed or not queries:
            return []
        tenant, section = parsed
        portal_no = portal(tenant, section)
        if not portal_no:
            logger.info("Taleo: %s has no search portal", spec)
            return []
        found: dict[str, dict] = {}
        for query in queries:
            for page in range(1, _MAX_PAGES_PER_QUERY + 1):
                try:
                    rows, total = search(tenant, section, portal_no, query, page)
                except Exception as exc:
                    logger.error("Taleo search error (%s / %r): %s", spec, query, exc)
                    break
                for row in rows:
                    job = _as_job(row, tenant, section, f"{tenant}/{section}")
                    if job:
                        found.setdefault(job["source_job_id"], job)
                if not rows or page * _PAGE_SIZE >= total:
                    break
        # Taleo's keyword search is loose ("Development Program: Operations
        # Pathway" for "software engineer"), and every job kept costs
        # enrichment a 400 KB page. The matcher's own filter fails open —
        # "Quality Engineer" passes for "Software Engineer" on the shared word
        # — so the titles are held to the stricter reading it ranks by.
        from app.services.matcher import title_priority_match

        return [j for j in found.values() if title_priority_match(j["title"], queries)]

    return fetch_boards_concurrently(company_slugs, _fetch_one, "Taleo", board_workers())
