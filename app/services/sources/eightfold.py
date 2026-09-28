"""
Eightfold career sites.

The careers platform behind Microsoft, Qualcomm, PayPal, Morgan Stanley, Eaton,
John Deere, Starbucks and Boston Scientific — ~6% of S&P 500 employers — on
`{company}.eightfold.ai` or on the company's own domain
(`apply.careers.microsoft.com`).

The older `/api/apply/v2/jobs` now answers "Not authorized for PCSX" on many
tenants. Every one of them serves the search its own page calls instead,
`/api/pcsx/search`, which the site's `robots.txt` explicitly allows and which
needs no token or cookie (measured on qualcomm.eightfold.ai, 2026-09-28):

    GET https://{host}/api/pcsx/search?domain={domain}&query=software+engineer&start=0

Ten results a call whatever page size is asked for, with title, locations,
posting time and a position URL. `domain` is the tenant's own name for itself
(`qualcomm.com`); the same `robots.txt` names it in its sitemap line, which is
where it is read from. Descriptions come from `/api/pcsx/position_details`,
spent on the titles matching wants; the posting page also carries a JobPosting
block, which is how enrichment fills in the rest.

A board is the careers host: `qualcomm.eightfold.ai`.
"""

import logging
import re
from collections import deque
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

from app.services.descriptions import clean as clean_description
from app.services.sources.base import (
    board_workers,
    company_from_host,
    fetch_boards_concurrently,
    parse_experience_level,
    rank_by_title,
)

logger = logging.getLogger(__name__)

_PAGE_SIZE = 10
_MAX_QUERIES = 8
_MAX_PAGES_PER_QUERY = 3
_MAX_DETAILS = 15
_TIMEOUT = 30

_HOST_RE = re.compile(r"^[a-z0-9-]+(?:\.[a-z0-9-]+)+$", re.I)
_DOMAIN_RE = re.compile(r"[?&]domain=([A-Za-z0-9.-]+)")
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def tenant_domain(host: str) -> str:
    """
    The `domain` a tenant's search wants: named by its robots.txt, else guessed.

    `qualcomm.eightfold.ai` names `qualcomm.com`; a tenant on its own domain is
    its own registrable domain (`apply.careers.microsoft.com` → `microsoft.com`).
    """
    try:
        resp = httpx.get(f"https://{host}/robots.txt", headers=_HEADERS,
                         timeout=_TIMEOUT, follow_redirects=True)
        match = _DOMAIN_RE.search(resp.text or "") if resp.status_code == 200 else None
        if match:
            return match.group(1).lower()
    except Exception:
        pass
    labels = host.lower().split(".")
    if host.lower().endswith(".eightfold.ai"):
        return f"{labels[0]}.com"
    return ".".join(labels[-2:])


def search(host: str, domain: str, query: str, start: int = 0) -> tuple[list[dict], int]:
    """One page (ten) of a search, and the total it reports."""
    url = (f"https://{host}/api/pcsx/search?domain={quote(domain)}"
           f"&query={quote(query)}&start={start}")
    resp = httpx.get(url, headers=_HEADERS, timeout=_TIMEOUT)
    resp.raise_for_status()
    data = (resp.json() or {}).get("data") or {}
    try:
        total = int(data.get("count") or 0)
    except (TypeError, ValueError):
        total = 0
    return [p for p in (data.get("positions") or []) if isinstance(p, dict)], total


def position_details(host: str, domain: str, position_id) -> dict:
    """One position's full record, or {}."""
    url = (f"https://{host}/api/pcsx/position_details?position_id={position_id}"
           f"&domain={quote(domain)}&hl=en")
    try:
        resp = httpx.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        return (resp.json() or {}).get("data") or {}
    except Exception as exc:
        logger.warning("Eightfold detail error (%s %s): %s", host, position_id, exc)
        return {}


def _posted(position: dict) -> str | None:
    stamp = position.get("postedTs") or position.get("creationTs")
    if not isinstance(stamp, (int, float)) or stamp <= 0:
        return None
    try:
        return datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def fetch(company_slugs: list[str], queries: list[str] | None = None) -> list[dict]:
    """Each tenant searched for each role, first pages before later ones."""
    queries = [q for q in dict.fromkeys(queries or []) if q.strip()][:_MAX_QUERIES]

    def _fetch_one(host: str) -> list[dict]:
        host = (host or "").strip().lower()
        if not _HOST_RE.match(host):
            logger.warning("Eightfold: not a careers host: %r", host)
            return []
        domain = tenant_domain(host)
        found: dict[str, dict] = {}
        pending = deque((q, 0) for q in queries)
        while pending:
            query, start = pending.popleft()
            try:
                rows, total = search(host, domain, query, start)
            except Exception as exc:
                logger.error("Eightfold search error (%s / %r): %s", host, query, exc)
                continue
            fresh = 0
            for row in rows:
                key = str(row.get("id") or "")
                if key and key not in found:
                    found[key] = row
                    fresh += 1
            nxt = start + len(rows)
            if (fresh and len(rows) == _PAGE_SIZE and nxt < total
                    and nxt // _PAGE_SIZE < _MAX_PAGES_PER_QUERY):
                pending.append((query, nxt))

        rows = list(found.values())
        described = {
            str(r.get("id")) for r in rank_by_title(rows, queries, lambda r: r.get("name"))
            [:_MAX_DETAILS]
        }
        company = company_from_host(host if not host.endswith(".eightfold.ai") else domain)
        jobs = []
        for row in rows:
            position_id = str(row.get("id") or "")
            title = (row.get("name") or "").strip()
            if not position_id or not title:
                continue
            description = ""
            if position_id in described:
                description = clean_description(
                    position_details(host, domain, position_id).get("jobDescription") or "")
            # `locations` over `standardizedLocations`: the second collapses some
            # places to a bare country code ("IT") or a native-script name.
            places = row.get("locations") or row.get("standardizedLocations") or []
            location = "; ".join(str(p) for p in places if p)[:300]
            mode = str(row.get("workLocationOption") or "")
            path = str(row.get("positionUrl") or f"/careers/job/{position_id}")
            jobs.append({
                "source": "eightfold",
                "source_job_id": f"{host}:{position_id}",
                "title": title,
                "company": company,
                "location": location,
                "is_remote": "remote" in f"{mode} {location}".lower(),
                "url": f"https://{host}{path}",
                "description": description,
                "experience_level": parse_experience_level(title, description),
                "posted_at": _posted(row),
            })
        return jobs

    return fetch_boards_concurrently(company_slugs, _fetch_one, "Eightfold", board_workers())
