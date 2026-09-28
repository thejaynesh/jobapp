"""
Paylocity job boards: `recruiting.paylocity.com/Recruiting/Jobs/All/<company id>`.

Paylocity runs payroll and hiring for small and mid-sized US employers; 57
rows of SimplifyJobs' lists apply through it, and Common Crawl's index names
3,335 of its boards (measured 2026-09-28). A board's page is rendered with
every opening embedded as the page's own data — no API to call:

    window.pageData = {"ModuleTitle": "Western National Group & Umialik Insurance",
                       "Jobs": [{"JobId": 4537849, "JobTitle": "IT Data Engineering Intern",
                                 "PublishedDate": "…", "JobLocation": {…}, "IsRemote": false,
                                 "Description": "<first 110 characters>"}, …]}

One request per company, then. The description is only a teaser; the posting
page carries the whole of it as JSON-LD, which enrichment's generic reader
already takes (6,616 characters, pay and location for the posting above).

A posting link (`/Recruiting/Jobs/Details/<id>`) does not say which board it
belongs to; only a board link does, so boards come from those links and from
Common Crawl. A board is its company id, lower-cased.
"""

import json
import logging
import re

import httpx

from app.services.descriptions import clean as clean_description
from app.services.sources.base import (
    board_workers,
    fetch_boards_concurrently,
    parse_experience_level,
    saw_postings,
)

logger = logging.getLogger(__name__)

_BASE = "https://recruiting.paylocity.com/Recruiting/Jobs"
_TIMEOUT = 30
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
}
_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_PAGE_DATA = re.compile(r"window\.pageData\s*=\s*(\{.*?\});\s*</script>", re.S)
_INTERN = re.compile(r"\bintern(ship)?s?\b", re.I)


def board_url(company_id: str) -> str:
    return f"{_BASE}/All/{company_id}"


def posting_url(job_id) -> str:
    return f"{_BASE}/Details/{job_id}"


def board(company_id: str) -> dict | None:
    """A board's embedded data, or None when Paylocity has no such board
    (it redirects an unknown one to a "job not found" page)."""
    company_id = (company_id or "").strip().lower()
    if not _GUID.match(company_id):
        logger.warning("Paylocity: not a company id: %r", company_id)
        return None
    resp = httpx.get(board_url(company_id), headers=_HEADERS, timeout=_TIMEOUT,
                     follow_redirects=True)
    resp.raise_for_status()
    match = _PAGE_DATA.search(resp.text)
    if not match:
        return None
    data = json.loads(match.group(1))
    return data if isinstance(data, dict) and data.get("ModuleId") is not None else None


def _location(job: dict) -> str:
    place = job.get("JobLocation") or {}
    country = place.get("Country")
    parts = [place.get("City"), place.get("State"),
             "United States" if country in ("USA", "US") else country]
    text = ", ".join(p for p in parts if p)
    return text or (job.get("LocationName") or "").strip()


def _as_job(job: dict, company: str) -> dict | None:
    job_id = job.get("JobId")
    title = (job.get("JobTitle") or "").strip()
    if not job_id or not title or job.get("IsInternal"):
        return None
    description = clean_description(job.get("Description") or "")
    location = _location(job)
    return {
        "source": "paylocity",
        "source_job_id": str(job_id),
        "title": title,
        "company": company,
        "location": location,
        "is_remote": bool(job.get("IsRemote")) or "remote" in location.lower(),
        "url": posting_url(job_id),
        # A teaser; enrichment reads the posting page's JSON-LD for the rest.
        "description": description,
        "experience_level": parse_experience_level(title, description),
        "posted_at": job.get("PublishedDate"),
        **({"employment_type": "internship"} if _INTERN.search(title) else {}),
    }


def fetch(company_slugs: list[str]) -> list[dict]:
    """Every public opening on each board."""

    def _fetch_one(company_id: str) -> list[dict]:
        data = board(company_id)
        if data is None:
            return []
        company = (data.get("ModuleTitle") or "").strip() or company_id
        # Internal postings are not open to us, so they count as not listed.
        saw_postings(str(job.get("JobId")) for job in data.get("Jobs") or []
                     if isinstance(job, dict) and job.get("JobId") and not job.get("IsInternal"))
        jobs = []
        for job in data.get("Jobs") or []:
            parsed = _as_job(job, company) if isinstance(job, dict) else None
            if parsed:
                jobs.append(parsed)
        return jobs

    return fetch_boards_concurrently(company_slugs, _fetch_one, "Paylocity", board_workers())
