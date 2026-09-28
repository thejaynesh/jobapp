"""
iCIMS careers-home sites (the "Jibe" front end), usually on the employer's own
domain.

The iCIMS adapter reads the classic portal (`careers-<company>.icims.com`),
which many large iCIMS customers no longer render: their postings live on a
branded careers site — `careers.amd.com`, `careers.jhuapl.edu`,
`careers.garmin.com` — which the classic reader cannot see. On SimplifyJobs'
new-grad lists those three alone carry 175 active postings.

Every such site serves the search its own page calls, no token needed
(measured on careers.amd.com, 2026-09-28):

    GET https://{host}/api/jobs?keywords=software+engineer&page=1&limit=100

100 a page, with the full description, qualifications, employer, location,
posting date — and `apply_url`, the classic iCIMS portal behind the site,
which discovery then registers too. These sites ask for a five-second crawl
delay in robots.txt, and requests to one site are spaced to honour it.

A board is the careers host: `careers.amd.com`.
"""

import logging
import re
import time
from collections import deque

import httpx

from app.services.descriptions import clean as clean_description
from app.services.sources.base import (
    _employment_type,
    board_workers,
    company_from_host,
    fetch_boards_concurrently,
    parse_experience_level,
    passing_titles,
)

logger = logging.getLogger(__name__)

_PAGE_SIZE = 100
_MAX_QUERIES = 5
_MAX_PAGES_PER_QUERY = 2
# What these sites' robots.txt asks for, between requests to one site.
CRAWL_DELAY_SECONDS = 5.0
_TIMEOUT = 30

_HOST_RE = re.compile(r"^[a-z0-9-]+(?:\.[a-z0-9-]+)+$", re.I)
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def search(host: str, keywords: str, page: int = 1,
           limit: int = _PAGE_SIZE) -> tuple[list[dict], int]:
    """One page of a keyword search, and the total it reports."""
    resp = httpx.get(f"https://{host}/api/jobs",
                     params={"keywords": keywords, "page": page, "limit": limit},
                     headers=_HEADERS, timeout=_TIMEOUT)
    resp.raise_for_status()
    data = resp.json() or {}
    rows = [(j.get("data") if isinstance(j, dict) and "data" in j else j)
            for j in (data.get("jobs") or [])]
    try:
        total = int(data.get("totalCount") or 0)
    except (TypeError, ValueError):
        total = 0
    return [r for r in rows if isinstance(r, dict)], total


def _description(row: dict) -> str:
    parts = [row.get("description") or ""]
    for label, key in (("Responsibilities", "responsibilities"),
                       ("Qualifications", "qualifications")):
        text = row.get(key)
        if isinstance(text, str) and text.strip() and text.strip() not in parts[0]:
            parts.append(f"<h3>{label}</h3>{text}")
    return clean_description("\n\n".join(p for p in parts if p))


def _location(row: dict) -> str:
    return (row.get("full_location")
            or ", ".join(p for p in (row.get("city"), row.get("state"), row.get("country")) if p)
            or row.get("location_name") or "").strip()


def fetch(company_slugs: list[str], queries: list[str] | None = None) -> list[dict]:
    """Each site searched for each role, first pages first, crawl delay kept."""
    queries = [q for q in dict.fromkeys(queries or []) if q.strip()][:_MAX_QUERIES]

    def _fetch_one(host: str) -> list[dict]:
        host = (host or "").strip().lower()
        if not _HOST_RE.match(host):
            logger.warning("Jibe: not a careers host: %r", host)
            return []
        found: dict[str, dict] = {}
        pending = deque((q, 1) for q in queries)
        first = True
        while pending:
            query, page = pending.popleft()
            if not first:
                time.sleep(CRAWL_DELAY_SECONDS)
            first = False
            try:
                rows, total = search(host, query, page, limit=_PAGE_SIZE)
            except Exception as exc:
                logger.error("Jibe search error (%s / %r): %s", host, query, exc)
                continue
            fresh = 0
            for row in rows:
                key = str(row.get("req_id") or row.get("slug") or "")
                if key and key not in found:
                    found[key] = row
                    fresh += 1
            if fresh and len(rows) == _PAGE_SIZE and page * _PAGE_SIZE < total \
                    and page < _MAX_PAGES_PER_QUERY:
                pending.append((query, page + 1))

        kept = passing_titles(list(found.values()), queries, lambda r: r.get("title"))
        fallback_company = company_from_host(host)
        jobs = []
        for row in kept:
            title = (row.get("title") or "").strip()
            job_id = str(row.get("slug") or row.get("req_id") or "").strip()
            if not title or not job_id:
                continue
            description = _description(row)
            location = _location(row)
            apply_url = str(row.get("apply_url") or "").strip()
            jobs.append({
                "source": "jibe",
                "source_job_id": f"{host}:{job_id}",
                "title": title,
                "company": (row.get("hiring_organization") or "").strip() or fallback_company,
                "location": location,
                "is_remote": "remote" in f"{row.get('location_type') or ''} {location}".lower(),
                "url": f"https://{host}/careers-home/jobs/{job_id}",
                **({"apply_url": apply_url} if apply_url.startswith("http") else {}),
                "description": description,
                "experience_level": parse_experience_level(title, description),
                "posted_at": row.get("posted_date") or row.get("create_date"),
                "employment_type": _employment_type(row.get("employment_type")),
            })
        return jobs

    return fetch_boards_concurrently(company_slugs, _fetch_one, "Jibe", board_workers())
