"""
JazzHR, read from the index it publishes of every open posting.

JazzHR hosts small and mid-sized US employers on `<company>.applytojob.com`:
264 rows of SimplifyJobs' lists point there, spread over as many companies,
which no board registry could ever enumerate one by one. It doesn't need to:
`app.jazz.co/robots.txt` names sitemaps that list every open JazzHR posting —
91,626 of them at 7,186 companies, 16 MB, read in four seconds (measured
2026-09-28) — and each posting URL carries its title:

    https://mobomo.applytojob.com/apply/1vgh8LVE6S/Front-End-Engineer

So the sitemaps say which companies have a posting worth reading, before any
company is asked. For those, one request returns every posting in full —
description, location, employment type and experience level:

    GET https://app.jazz.co/feeds/export/jobs/<company>

Only titles the matcher would rank first for the profile's roles are pursued
(`matcher.title_priority_match`; the looser filter passes "Field Service
Engineer" for "Software Engineer" and doubles the companies). Of those, a
posting this process has already read is not pursued again for a week — the
job is stored, and nothing closes a job for not being re-sighted (the
liveness sweep does that by asking the posting) — so after the first run each
one reads only the companies with something new, up to a cap.

Which companies first: the most new postings carrying every word of a role
("Senior Software Engineer" for "Software Engineer"), then the most that
merely share one. A plain count put an inspections firm with 171 "Vacancy
Data Driver" postings ahead of every software company for "Data Scientist".

A posting's code in the sitemap is not always the one its export gives (a
long hex form against a short one, for 11 of 117 postings at one company),
so a company's rows are judged by their own titles too, and once its export
is read every code the sitemap gave for it counts as read.

A posting ID embeds when it was created (`job_20221109133627_…`), which is
when JazzHR published it, the same meaning as Lever's `createdAt`.
"""

import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import httpx

from app.services.descriptions import clean as clean_description
from app.services.sources.base import (
    _employment_type,
    board_workers,
    fetch_boards_concurrently,
    parse_experience_level,
)

logger = logging.getLogger(__name__)

ROBOTS_URL = "https://app.jazz.co/robots.txt"
EXPORT_URL = "https://app.jazz.co/feeds/export/jobs/{company}"
DEFAULT_MAX_COMPANIES = 150
_MAX_SITEMAPS = 20
_TIMEOUT = 60
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
}

_SITEMAP_LINE = re.compile(r"^\s*Sitemap:\s*(https?://app\.jazz\.co/feeds/google/xml/\d+)\s*$",
                           re.I | re.M)
_POSTING = re.compile(
    r"<loc>\s*https?://([a-z0-9-]+)\.applytojob\.com/apply/([A-Za-z0-9]+)/([^?<\s]*)", re.I)
_CREATED = re.compile(r"^job_(\d{14})_")

# posting code → when this process last read it.
_SEEN: dict[str, float] = {}
_SEEN_SECONDS = 7 * 24 * 3600
_SEEN_SIZE = 200_000

_EXPERIENCE = {
    "internship": "entry", "entry level": "entry",
    "senior level": "senior", "manager": "senior", "executive": "senior",
    "director": "senior",
}


def sitemap_urls() -> list[str]:
    """The sitemaps JazzHR's robots.txt names, in order."""
    resp = httpx.get(ROBOTS_URL, headers=_HEADERS, timeout=_TIMEOUT)
    resp.raise_for_status()
    urls = [u.replace("http://", "https://", 1) for u in _SITEMAP_LINE.findall(resp.text)]
    return list(dict.fromkeys(urls))[:_MAX_SITEMAPS]


def sitemap_postings() -> list[dict]:
    """Every open posting the sitemaps list: company, code and title."""
    postings: dict[str, dict] = {}
    for url in sitemap_urls():
        try:
            resp = httpx.get(url, headers=_HEADERS, timeout=_TIMEOUT)
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("JazzHR: sitemap %s unavailable: %s", url, exc)
            continue
        for company, code, slug in _POSTING.findall(resp.text):
            postings.setdefault(code, {"company": company.lower(), "code": code,
                                       "title": slug.replace("-", " ").strip()})
    return list(postings.values())


def _wanted(postings: list[dict], queries: list[str]) -> list[dict]:
    """The postings whose titles the matcher ranks first for these roles."""
    from app.services.matcher import title_priority_match

    return [p for p in postings if p["title"] and title_priority_match(p["title"], queries)]


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9+#]+", (text or "").lower()))


def _complete(title: str, queries: list[str]) -> bool:
    """Whether the title carries every word of at least one role."""
    words = _words(title)
    return any(_words(q) and _words(q) <= words for q in queries)


def export(company: str) -> tuple[str, list[dict]]:
    """A company's name and every open posting, in full."""
    resp = httpx.get(EXPORT_URL.format(company=company), headers=_HEADERS, timeout=_TIMEOUT)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    name = (root.findtext("company") or "").strip()
    rows = []
    for job in root.iter("job"):
        rows.append({child.tag: (child.text or "").strip() for child in job})
    return name, rows


def _posted_at(job_id: str) -> str | None:
    match = _CREATED.match(job_id or "")
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%d%H%M%S").replace(
            tzinfo=timezone.utc).isoformat()
    except ValueError:
        return None


def _code(url: str) -> str:
    match = re.search(r"/apply/([A-Za-z0-9]+)", url or "")
    return match.group(1) if match else ""


def _as_job(row: dict, company: str) -> dict | None:
    title = row.get("title") or ""
    url = row.get("url") or ""
    job_id = row.get("id") or ""
    if not title or not url or not job_id or (row.get("status") or "Open").lower() != "open":
        return None
    description = clean_description(row.get("description") or "")
    location = ", ".join(p for p in (row.get("city"), row.get("state"), row.get("country")) if p)
    experience = (row.get("experience") or "").strip().lower()
    intern = experience == "internship" or bool(re.search(r"\bintern", title, re.I))
    return {
        "source": "jazzhr",
        "source_job_id": job_id,
        "title": title,
        "company": company,
        "location": location,
        "is_remote": "remote" in f"{title} {location}".lower(),
        "url": url,
        "description": description,
        "experience_level": _EXPERIENCE.get(experience) or parse_experience_level(title, description),
        "posted_at": _posted_at(job_id),
        "employment_type": "internship" if intern else _employment_type(row.get("type")),
    }


def _remember(code: str, when: float) -> None:
    if len(_SEEN) >= _SEEN_SIZE:
        for stale in sorted(_SEEN, key=_SEEN.get)[: _SEEN_SIZE // 10]:
            del _SEEN[stale]
    _SEEN[code] = when


def fetch(queries: list[str], max_companies: int | None = None) -> list[dict]:
    """Every posting matching the roles at the companies with something new."""
    queries = [q for q in dict.fromkeys(queries or []) if q and q.strip()]
    if not queries:
        return []
    cap = DEFAULT_MAX_COMPANIES if max_companies is None else max(0, int(max_companies))
    now = time.monotonic()

    def fresh(key: str) -> bool:
        seen_at = _SEEN.get(key)
        return seen_at is None or now - seen_at > _SEEN_SECONDS

    from app.services.matcher import title_priority_match

    wanted = _wanted(sitemap_postings(), queries)
    by_company: dict[str, list[dict]] = {}
    for posting in wanted:
        if fresh(posting["code"]):
            by_company.setdefault(posting["company"], []).append(posting)
    wanted_codes = {p["code"] for p in wanted}

    def priority(company: str) -> tuple:
        new = by_company[company]
        return (-sum(_complete(p["title"], queries) for p in new), -len(new), company)

    companies = sorted(by_company, key=priority)[:cap]
    logger.info("JazzHR: %d matching postings; %d companies with new ones, reading %d",
                len(wanted), len(by_company), len(companies))

    def _fetch_one(company: str) -> list[dict]:
        name, rows = export(company)
        jobs = []
        for row in rows:
            code = _code(row.get("url") or "")
            title = row.get("title") or ""
            if code not in wanted_codes and not title_priority_match(title, queries):
                continue
            key = row.get("id") or code
            if not fresh(key):
                continue
            job = _as_job(row, name or company)
            if job:
                jobs.append(job)
                _remember(key, now)
                _remember(code, now)
        for posting in by_company.get(company, []):
            _remember(posting["code"], now)
        return jobs

    return fetch_boards_concurrently(companies, _fetch_one, "JazzHR", board_workers())
