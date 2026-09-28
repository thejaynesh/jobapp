"""
Phenom career sites.

Phenom is the careers-site layer on ~13% of S&P 500 employers (Mastercard,
Cisco, HPE, eBay…), usually on the company's own domain and usually in front of
another ATS — Mastercard's postings apply through its Workday board. So a
Phenom site gives both its postings and, through `applyUrl`, the board behind
them, which discovery then registers for direct polling.

Every Phenom site answers the same search call its own page makes, with no
token or CSRF header (measured on careers.mastercard.com, 2026-09-28):

    POST https://{host}/widgets
    {"ddoKey": "refineSearch", "lang": "en_us", "country": "us",
     "keywords": "software engineer", "from": 0, "size": 50, ...}

50 a page, with title, location, posting date and apply link. Descriptions come
from `ddoKey: "jobDetail"` (a large response), spent on the titles matching
wants; the posting page also carries a JobPosting block, which is how
enrichment fills in the rest.

A board is `host/country/lang` — `careers.mastercard.com/us/en` — read out of
a posting link (`…/us/en/job/R-275650/…`).
"""

import logging
import re
from collections import deque

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

_PAGE_SIZE = 50
_MAX_QUERIES = 8
_MAX_PAGES_PER_QUERY = 2
_MAX_DETAILS = 15
_TIMEOUT = 30

_SPEC_RE = re.compile(r"^([a-z0-9-]+(?:\.[a-z0-9-]+)+)/([a-z]{2})/([a-z]{2})$", re.I)
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def parse_spec(spec: str) -> tuple[str, str, str] | None:
    """`host/country/lang`, or None."""
    match = _SPEC_RE.match((spec or "").strip())
    if not match:
        return None
    host, country, lang = match.groups()
    return host.lower(), country.lower(), lang.lower()


def posting_url(host: str, country: str, lang: str, job_id: str) -> str:
    return f"https://{host}/{country}/{lang}/job/{job_id}"


def _widgets(host: str, body: dict) -> dict:
    resp = httpx.post(f"https://{host}/widgets", json=body, headers=_HEADERS,
                      timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json() or {}


def search(host: str, country: str, lang: str, keywords: str,
           offset: int = 0, size: int = _PAGE_SIZE) -> tuple[list[dict], int]:
    """One page of a keyword search, and the total it reports."""
    data = _widgets(host, {
        "lang": f"{lang}_{country}", "deviceType": "desktop", "country": country,
        "pageName": "search-results", "ddoKey": "refineSearch", "from": offset,
        "size": size, "jobs": True, "counts": False, "keywords": keywords,
        "global": True, "selected_fields": {}, "siteType": "external",
    })
    block = data.get("refineSearch") or {}
    jobs = (block.get("data") or {}).get("jobs") or []
    try:
        total = int(block.get("totalHits") or 0)
    except (TypeError, ValueError):
        total = 0
    return [j for j in jobs if isinstance(j, dict)], total


def job_detail(host: str, country: str, lang: str, job_seq_no: str) -> dict:
    """The posting's full record, or {}."""
    try:
        data = _widgets(host, {
            "lang": f"{lang}_{country}", "deviceType": "desktop", "country": country,
            "pageName": "job", "ddoKey": "jobDetail", "jobSeqNo": job_seq_no,
            "siteType": "external",
        })
    except Exception as exc:
        logger.warning("Phenom detail error (%s %s): %s", host, job_seq_no, exc)
        return {}
    return ((data.get("jobDetail") or {}).get("data") or {}).get("job") or {}


def _location(job: dict) -> str:
    return (job.get("cityStateCountry") or job.get("location")
            or ", ".join(p for p in (job.get("city"), job.get("state"), job.get("country")) if p)
            or "").strip()


def fetch(company_slugs: list[str], queries: list[str] | None = None) -> list[dict]:
    """Each site searched for each role, first pages before second ones."""
    queries = [q for q in dict.fromkeys(queries or []) if q.strip()][:_MAX_QUERIES]

    def _fetch_one(spec: str) -> list[dict]:
        parsed = parse_spec(spec)
        if not parsed:
            logger.warning("Phenom: not a host/country/lang spec: %r", spec)
            return []
        host, country, lang = parsed
        found: dict[str, dict] = {}
        pending = deque((q, 0) for q in queries)
        while pending:
            query, offset = pending.popleft()
            try:
                rows, total = search(host, country, lang, query, offset, size=_PAGE_SIZE)
            except Exception as exc:
                logger.error("Phenom search error (%s / %r): %s", spec, query, exc)
                continue
            fresh = 0
            for row in rows:
                key = str(row.get("jobSeqNo") or row.get("jobId") or "")
                if key and key not in found:
                    found[key] = row
                    fresh += 1
            nxt = offset + len(rows)
            if (fresh and len(rows) == _PAGE_SIZE and nxt < total
                    and nxt // _PAGE_SIZE < _MAX_PAGES_PER_QUERY):
                pending.append((query, nxt))

        rows = list(found.values())
        described = {
            id(r) for r in rank_by_title(rows, queries, lambda r: r.get("title"))[:_MAX_DETAILS]
        }
        fallback_company = company_from_host(host)
        jobs = []
        for row in rows:
            title = (row.get("title") or "").strip()
            job_id = str(row.get("jobId") or row.get("reqId") or "").strip()
            if not title or not job_id:
                continue
            description, company = "", ""
            if id(row) in described and row.get("jobSeqNo"):
                record = job_detail(host, country, lang, str(row["jobSeqNo"]))
                description = clean_description(record.get("description") or "")
                company = (record.get("companyName") or "").strip()
            location = _location(row)
            apply_url = str(row.get("applyUrl") or "").strip()
            jobs.append({
                "source": "phenom",
                "source_job_id": f"{host}:{job_id}",
                "title": title,
                "company": company or fallback_company,
                "location": location,
                "is_remote": "remote" in f"{title} {location}".lower(),
                "url": posting_url(host, country, lang, job_id),
                # The board behind the site, when there is one — discovery reads
                # it, and enrichment can read that board's API.
                **({"apply_url": apply_url} if apply_url.startswith("http") else {}),
                "description": description,
                "experience_level": parse_experience_level(title, description),
                "posted_at": row.get("postedDate") or row.get("dateCreated"),
            })
        return jobs

    return fetch_boards_concurrently(company_slugs, _fetch_one, "Phenom", board_workers())
