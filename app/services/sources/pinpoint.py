"""
Pinpoint ATS boards (`<slug>.pinpointhq.com`).

Each board serves its whole state as one JSON document (checked on
wolve.pinpointhq.com, 2026-09-28):

    GET https://{slug}.pinpointhq.com/postings.json

Every posting with its description, responsibilities, skills, workplace type,
employment type and — where the employer shows it — the pay band. One request
per company, nothing more to fetch.
"""

import logging

import httpx

from app.services.descriptions import clean as clean_description
from app.services.sources.base import (
    _employment_type,
    board_workers,
    fetch_boards_concurrently,
    parse_experience_level,
)

logger = logging.getLogger(__name__)

_TIMEOUT = 20
_PERIODS = {"year": "year", "yearly": "year", "annually": "year", "month": "month",
            "monthly": "month", "week": "week", "weekly": "week", "day": "day",
            "daily": "day", "hour": "hour", "hourly": "hour"}


def postings(slug: str) -> list[dict]:
    resp = httpx.get(f"https://{slug}.pinpointhq.com/postings.json", timeout=_TIMEOUT,
                     follow_redirects=True)
    resp.raise_for_status()
    return [p for p in ((resp.json() or {}).get("data") or []) if isinstance(p, dict)]


def _pay(row: dict) -> dict:
    if not row.get("compensation_visible"):
        return {}
    low, high = row.get("compensation_minimum"), row.get("compensation_maximum")
    if low is None and high is None:
        return {}
    period = _PERIODS.get(str(row.get("compensation_frequency") or "").lower())
    currency = (row.get("compensation_currency") or "").upper() or None
    if not period or not currency:
        return {}
    return {"salary_min": low, "salary_max": high, "salary_currency": currency,
            "salary_period": period}


def _as_job(slug: str, row: dict) -> dict | None:
    title = (row.get("title") or "").strip()
    url = (row.get("url") or "").strip()
    if not title or not url:
        return None
    parts = [row.get("description") or ""]
    for header, key in (("key_responsibilities_header", "key_responsibilities"),
                        ("skills_knowledge_expertise_header", "skills_knowledge_expertise"),
                        ("benefits_header", "benefits")):
        if row.get(key):
            parts.append(f"<h3>{row.get(header) or ''}</h3>{row[key]}")
    description = clean_description("\n\n".join(p for p in parts if p))
    place = row.get("location") if isinstance(row.get("location"), dict) else {}
    location = (place.get("name") or "").strip()
    workplace = str(row.get("workplace_type") or "")
    return {
        "source": "pinpoint",
        "source_job_id": f"{slug}:{row.get('id')}" if row.get("id") else None,
        "title": title,
        "company": slug,
        "location": location,
        "is_remote": workplace.lower() == "remote" or "remote" in location.lower(),
        "url": url,
        "description": description,
        "experience_level": parse_experience_level(title, description),
        "posted_at": None,
        "employment_type": _employment_type(row.get("employment_type")),
        **_pay(row),
    }


def fetch(company_slugs: list[str]) -> list[dict]:
    def _fetch_one(slug: str) -> list[dict]:
        return [job for job in (_as_job(slug, row) for row in postings(slug)) if job]

    return fetch_boards_concurrently(company_slugs, _fetch_one, "Pinpoint", board_workers())
