"""
Oracle Recruiting Cloud (Oracle Fusion HCM) career sites.

The ATS behind American Express, JPMorgan Chase, Texas Instruments, Emerson,
Navy Federal and several hundred other large US employers: 479 of the active
new-grad and internship postings on SimplifyJobs' lists apply through one of
176 Oracle hosts (measured 2026-09-28), and it had no adapter here.

Every Oracle careers site is a single-page app on `…oraclecloud.com` that
reads one public REST resource, without a login:

    GET https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions
        ?onlyData=true&expand=requisitionList.secondaryLocations
        &finder=findReqs;siteNumber={site},limit=200,offset=N,sortBy=POSTING_DATES_DESC

`limit` is honoured up to 200 (AmEx's 497 openings take three requests). The
listing has id, title, posting date, location and workplace type; the
description is one more request per posting
(`recruitingCEJobRequisitionDetails`), spent on the titles matching wants.
Oracle's own docs mark the resource "for Oracle internal use"; it is what every
one of these careers pages calls.

A board is `host:site` — `egug.fa.us2.oraclecloud.com:CX_1` — read straight out
of a posting link (`…/hcmUI/CandidateExperience/en/sites/CX_1/job/26007181`).
"""

import html
import logging
import re

import httpx

from app.services.descriptions import clean as clean_description
from app.services.sources.base import (
    board_workers,
    fetch_boards_concurrently,
    parse_experience_level,
    passing_titles,
    rank_by_title,
)

logger = logging.getLogger(__name__)

_API = "https://{host}/hcmRestApi/resources/latest"
_PAGE_SIZE = 200
_MAX_PAGES = 5            # a thousand openings a site
_MAX_DETAILS = 20         # descriptions per site per cycle; enrichment does the rest
_TIMEOUT = 30

_HOST_RE = re.compile(r"^[a-z0-9-]+(?:\.[a-z0-9-]+)*\.oraclecloud\.com$", re.I)
_SITE_RE = re.compile(r"^[A-Za-z0-9_]+$")
_TITLE_RE = re.compile(r"<title>\s*([^<]{2,120}?)\s*</title>", re.I)


def parse_spec(spec: str) -> tuple[str, str] | None:
    """`host:site`, or None for anything that is not an Oracle career site."""
    host, _, site = (spec or "").strip().partition(":")
    host = host.lower()
    if _HOST_RE.match(host) and _SITE_RE.match(site):
        return host, site
    return None


def posting_url(host: str, site: str, job_id: str) -> str:
    return f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{job_id}"


def list_page(host: str, site: str, offset: int = 0, limit: int = _PAGE_SIZE,
              keyword: str | None = None) -> tuple[list[dict], int]:
    """One page of a site's requisitions, and the site's total."""
    finder = f"findReqs;siteNumber={site},limit={limit},offset={offset},sortBy=POSTING_DATES_DESC"
    if keyword:
        finder += f',keyword="{keyword}"'
    url = (f"{_API.format(host=host)}/recruitingCEJobRequisitions?onlyData=true"
           f"&expand=requisitionList.secondaryLocations&finder={finder}")
    resp = httpx.get(url, timeout=_TIMEOUT)
    resp.raise_for_status()
    items = (resp.json() or {}).get("items") or []
    if not items:
        return [], 0
    search = items[0]
    try:
        total = int(search.get("TotalJobsCount") or 0)
    except (TypeError, ValueError):
        total = 0
    return list(search.get("requisitionList") or []), total


def detail(host: str, site: str, job_id: str) -> dict:
    """The requisition's full record, or {} if it could not be read."""
    url = (f"{_API.format(host=host)}/recruitingCEJobRequisitionDetails?expand=all"
           f'&onlyData=true&finder=ById;Id="{job_id}",siteNumber={site}')
    try:
        resp = httpx.get(url, timeout=_TIMEOUT)
        resp.raise_for_status()
        items = (resp.json() or {}).get("items") or []
        return items[0] if items else {}
    except Exception as exc:
        logger.warning("Oracle detail error (%s %s): %s", host, job_id, exc)
        return {}


def description_of(record: dict) -> str:
    """Description, responsibilities and qualifications, which Oracle keeps apart."""
    parts = [record.get(key) or "" for key in (
        "ExternalDescriptionStr", "ExternalResponsibilitiesStr",
        "ExternalQualificationsStr", "CorporateDescriptionStr",
    )]
    return clean_description("\n\n".join(p for p in parts if p.strip()))


def site_name(host: str, site: str) -> str:
    """The employer, from the careers page's own title ("American Express")."""
    try:
        resp = httpx.get(f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}",
                         timeout=_TIMEOUT, follow_redirects=True)
        match = _TITLE_RE.search(resp.text or "")
    except Exception:
        return ""
    name = html.unescape(match.group(1)).strip() if match else ""
    return "" if name.lower() in {"careers", "oracle", "candidate experience"} else name


def _location(row: dict) -> str:
    places = [row.get("PrimaryLocation") or ""]
    places += [loc.get("Name") or "" for loc in (row.get("secondaryLocations") or [])
               if isinstance(loc, dict)]
    return "; ".join(p for p in dict.fromkeys(places) if p)[:300]


def fetch(company_slugs: list[str], queries: list[str] | None = None) -> list[dict]:
    """
    Every listed opening on each Oracle site whose title matching would keep.

    The whole site is listed (a few requests), not searched: Oracle's keyword
    search matches on words anywhere in the posting, and a site of five
    hundred is cheaper to read than to query ten ways.
    """
    queries = list(queries or [])

    def _fetch_one(spec: str) -> list[dict]:
        parsed = parse_spec(spec)
        if not parsed:
            logger.warning("Oracle: not a host:site spec: %r", spec)
            return []
        host, site = parsed
        rows: list[dict] = []
        offset = 0
        for _ in range(_MAX_PAGES):
            page, total = list_page(host, site, offset, limit=_PAGE_SIZE)
            rows.extend(page)
            offset += len(page)
            if not page or offset >= total:
                break

        wanted = passing_titles(rows, queries, lambda r: r.get("Title"))
        if not wanted:
            return []
        described = {
            str(r.get("Id")) for r in rank_by_title(wanted, queries, lambda r: r.get("Title"))
            [:_MAX_DETAILS]
        }
        company = site_name(host, site)

        jobs = []
        for row in wanted:
            job_id = str(row.get("Id") or "").strip()
            title = (row.get("Title") or "").strip()
            if not job_id or not title:
                continue
            description = ""
            posted = row.get("PostedDate")
            if job_id in described:
                record = detail(host, site, job_id)
                description = description_of(record)
                posted = record.get("ExternalPostedStartDate") or posted
            location = _location(row)
            workplace = str(row.get("WorkplaceTypeCode") or row.get("WorkplaceType") or "")
            jobs.append({
                "source": "oracle",
                # Requisition numbers are per tenant, so the host is part of it.
                "source_job_id": f"{host}:{job_id}",
                "title": title,
                "company": company,
                "location": location,
                "is_remote": "remote" in f"{workplace} {location}".lower(),
                "url": posting_url(host, site, job_id),
                "description": description,
                "experience_level": parse_experience_level(title, description),
                "posted_at": posted,
            })
        return jobs

    return fetch_boards_concurrently(company_slugs, _fetch_one, "Oracle", board_workers())
