"""
TikTok's own careers search, read by the server.

TikTok was the largest single employer missing from what we read: 161 of
the 1,983 active new-grad and internship postings SimplifyJobs listed over the
last 60 days (measured 2026-09-28) link to lifeattiktok.com, and nothing else
we poll carries them. The site's search reads a public endpoint, with no
key, token or session — only a header naming which of the company's portals
to search:

    POST https://api.lifeattiktok.com/api/v1/public/supplier/search/job/posts
    website-path: tiktok
    {"keyword": "software engineer", "limit": 100, "offset": 0,
     "location_code_list": [<city codes>], ...}

100 a page with the full description and qualifications inline. The city
codes come from the same site's filter list, where every city carries its
country; restricting to the profile's countries server-side is the difference
between 441 US matches for "software engineer" and 760 worldwide.

There is no posting date: a posting's ID encodes when its requisition was
created, but TikTok keeps evergreen requisitions open for years (IDs from
2024 on postings SimplifyJobs saw this spring), so that date would age live
roles out. Left empty, the job counts from when we first saw it.

ByteDance's own site (joinbytedance.com) runs the same software but refuses
its API to anything that is not its page, so it is not read here.
"""

import logging

import httpx

from app.services.descriptions import clean as clean_description
from app.services.sources.base import parse_experience_level, raise_if_blocked

logger = logging.getLogger(__name__)

_API = "https://api.lifeattiktok.com/api/v1/public/supplier"
_SITE = "https://lifeattiktok.com"
_PAGE_SIZE = 100
# There is no newest-first order, so a search is only complete when read to
# its end; the largest role search ("software engineer", 441 US) is 5 pages.
DEFAULT_MAX_PAGES = 5
_TIMEOUT = 30
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Accept-Language": "en-US",
    # Which portal to search; the site's own requests carry it.
    "website-path": "tiktok",
}
# Recruitment types: 1xx experienced, 2xx campus (201 graduate, 202 intern).
_CAMPUS_PREFIX = "2"
_INTERN = "202"

# The two-letter codes the rest of the fetcher uses, as TikTok names countries.
_COUNTRY_NAMES = {
    "us": "United States of America", "ca": "Canada", "gb": "United Kingdom",
    "ie": "Ireland", "de": "Germany", "fr": "France", "nl": "Netherlands",
    "es": "Spain", "it": "Italy", "pl": "Poland", "in": "India", "au": "Australia",
    "jp": "Japan", "sg": "Singapore", "mx": "Mexico", "br": "Brazil",
}


def _post(path: str, body: dict) -> dict:
    resp = httpx.post(f"{_API}{path}", json=body, headers=_HEADERS, timeout=_TIMEOUT)
    raise_if_blocked(resp, "TikTok")
    resp.raise_for_status()
    data = resp.json() or {}
    if data.get("code") not in (0, None):
        raise RuntimeError(f"TikTok API error {data.get('code')}: {data.get('message')}")
    return data.get("data") or {}


def _chain(node: dict | None) -> list[dict]:
    """A city and its parents, city first: city, state, country."""
    out = []
    while isinstance(node, dict) and len(out) < 5:
        out.append(node)
        node = node.get("parent")
    return out


def city_codes(country_codes: list[str] | None) -> list[str]:
    """TikTok's codes for every city it hires in within the given countries
    (the US when none are given). Empty when it has no office in any of them."""
    wanted = {_COUNTRY_NAMES[c.lower()] for c in (country_codes or [])
              if c.lower() in _COUNTRY_NAMES} or {_COUNTRY_NAMES["us"]}
    cities = _post("/config/job/filters", {}).get("city_list") or []
    return [c["code"] for c in cities
            if isinstance(c, dict) and c.get("code")
            and (_chain(c)[-1].get("en_name") in wanted)]


def search(keyword: str, cities: list[str], offset: int = 0,
           limit: int = _PAGE_SIZE) -> tuple[list[dict], int]:
    """One page of a search, and the total it reports."""
    data = _post("/search/job/posts", {
        "keyword": keyword, "limit": limit, "offset": offset,
        "recruitment_id_list": [], "job_category_id_list": [],
        "subject_id_list": [], "location_code_list": list(cities),
    })
    rows = [r for r in (data.get("job_post_list") or []) if isinstance(r, dict)]
    try:
        total = int(data.get("count") or 0)
    except (TypeError, ValueError):
        total = 0
    return rows, total


def _location(row: dict) -> str:
    names = [n.get("en_name") for n in _chain(row.get("city_info")) if n.get("en_name")]
    names = ["United States" if n == "United States of America" else n for n in names]
    # "Singapore, Singapore, Singapore" is one place said three times.
    return ", ".join(dict.fromkeys(names))


def _as_job(row: dict) -> dict | None:
    job_id = str(row.get("id") or "").strip()
    title = (row.get("title") or "").strip()
    if not job_id or not title:
        return None
    parts = [row.get("description") or ""]
    if row.get("requirement"):
        parts.append(f"Qualifications\n{row['requirement']}")
    description = clean_description("\n\n".join(p for p in parts if p))
    location = _location(row)
    kind = str((row.get("recruit_type") or {}).get("id") or "")
    campus = kind.startswith(_CAMPUS_PREFIX)
    subject = str((row.get("job_subject") or {}).get("en_name") or "")
    intern = kind == _INTERN or "intern" in f"{title} {subject}".lower()
    return {
        "source": "tiktok",
        "source_job_id": job_id,
        "title": title,
        "company": "TikTok",
        "location": location,
        "is_remote": "remote" in f"{title} {location}".lower(),
        "url": f"{_SITE}/search/{job_id}",
        "description": description,
        # Campus hiring is new graduates and interns, whatever the title says.
        "experience_level": "entry" if campus else parse_experience_level(title, description),
        "posted_at": None,
        **({"employment_type": "internship"} if intern else {}),
    }


def fetch(query: str, cities: list[str], max_pages: int | None = None) -> list[dict]:
    """Up to `max_pages` pages of one search, restricted to `cities`."""
    if not cities:
        return []
    pages = DEFAULT_MAX_PAGES if max_pages is None else max(1, int(max_pages))
    jobs: dict[str, dict] = {}
    for page in range(pages):
        try:
            rows, total = search(query, cities, offset=page * _PAGE_SIZE, limit=_PAGE_SIZE)
        except Exception as exc:
            if not page:
                raise
            # A later page failing leaves the earlier ones worth keeping.
            logger.warning("TikTok: page %d of %r failed: %s", page + 1, query, exc)
            break
        for row in rows:
            job = _as_job(row)
            if job:
                jobs.setdefault(job["source_job_id"], job)
        if len(rows) < _PAGE_SIZE or (page + 1) * _PAGE_SIZE >= total:
            break
    logger.info("TikTok: %d jobs for %r", len(jobs), query)
    return list(jobs.values())
