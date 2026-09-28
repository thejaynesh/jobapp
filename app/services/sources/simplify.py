"""
SimplifyJobs' curated US early-career postings, from the machine-readable file
behind its GitHub lists.

`SimplifyJobs/New-Grad-Positions` (and its internship sibling) keep every
posting they have ever listed in `.github/scripts/listings.json` beside the
README people read. Measured 2026-09-28: 3,062 active new-grad postings, 210 of
them posted in the previous seven days, about 89% in the US — each with a real
posting date and a direct link to the employer's ATS, which is where enrichment
reads the description from.

The README was already mined for company slugs (`SLUG_HARVEST_URLS`). The
postings themselves were thrown away, and so was everything the README hides:
inactive rows, which name thousands more company boards. `rows()` serves both
uses, so the job source and board discovery read the file the same way.

Two things the file has that are *not* worth trusting: `sponsorship` is
"Other" on 99.7% of active rows, so it is not read; and `source` names the
contributor, not the board.
"""

import logging
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from app.services.sources.base import age_cutoff, parse_experience_level

logger = logging.getLogger(__name__)

NEW_GRAD_URL = (
    "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/"
    ".github/scripts/listings.json"
)
INTERNSHIPS_URL = (
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/"
    ".github/scripts/listings.json"
)

# Query parameters Simplify adds to an employer's link to mark where it found
# it. They are not part of the posting's address, and leaving them on gives the
# same posting two URLs — one from here and one from the board itself — which
# is one of the three things dedupe matches on. Everything else (`gh_jid`,
# `job`, `token`) *is* the address and stays.
_MARKER_PARAMS = {"ats", "icims", "ref", "embed", "mobile", "needsredirect"}

_REMOTE_WORDS = ("remote",)


def rows(url: str, timeout: int = 60) -> list[dict]:
    """Every row of one listings file. Raises on a failed download."""
    resp = httpx.get(url, timeout=timeout, follow_redirects=True)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        raise ValueError(f"{url}: expected a JSON list, got {type(data).__name__}")
    return [row for row in data if isinstance(row, dict)]


def clean_url(url: str) -> str:
    """The employer's link without Simplify's own markers."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    query = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _MARKER_PARAMS and not k.lower().startswith("utm_")
    ]
    return urlunsplit(parts._replace(query=urlencode(query)))


def _posted_at(row: dict) -> str | None:
    stamp = row.get("date_posted")
    if not isinstance(stamp, (int, float)) or stamp <= 0:
        return None
    try:
        return datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _as_job(row: dict, internship: bool) -> dict | None:
    title = str(row.get("title") or "").strip()
    company = str(row.get("company_name") or "").strip()
    url = str(row.get("url") or "").strip()
    if not title or not company or not url.startswith("http"):
        return None
    places = [str(p).strip() for p in (row.get("locations") or []) if str(p).strip()]
    location = "; ".join(places[:6])
    return {
        "source": "simplify",
        "source_job_id": str(row.get("id") or "") or None,
        "title": title,
        "company": company,
        "location": location,
        "is_remote": any(w in location.lower() for w in _REMOTE_WORDS),
        "url": clean_url(url),
        # A list row is not a description. The URL is the employer's ATS, and
        # enrichment reads Workday, Greenhouse, Lever, Ashby, Oracle and the
        # rest by API from there.
        "description": "",
        # Everything on these lists is early career: that is what they are.
        # The title can still say otherwise ("Senior" does appear), and when it
        # does the title wins.
        "experience_level": parse_experience_level(title, "") or "entry",
        "posted_at": _posted_at(row),
        **({"employment_type": "internship"} if internship else {}),
    }


def fetch(urls: list[str], max_age_days=None) -> list[dict]:
    """
    Active postings from each listings file, newest first.

    A file that fails to download is logged and skipped; the others still
    count. The age window is the settings page's own ("Maximum job age"), so a
    row older than that is not handed to the fetcher only to be dropped there.
    """
    cutoff = age_cutoff(max_age_days)
    jobs: dict[str, dict] = {}
    for url in urls:
        try:
            listed = rows(url)
        except Exception as exc:
            logger.error("Simplify: could not read %s: %s", url, exc)
            continue
        internship = "intern" in url.lower()
        kept = 0
        for row in listed:
            if not row.get("active") or row.get("is_visible") is False:
                continue
            job = _as_job(row, internship)
            if not job:
                continue
            if cutoff is not None and job["posted_at"]:
                if datetime.fromisoformat(job["posted_at"]) < cutoff:
                    continue
            key = job["source_job_id"] or job["url"]
            if key not in jobs:
                jobs[key] = job
                kept += 1
        logger.info("Simplify: %d active postings from %s", kept, url)
    return sorted(jobs.values(), key=lambda j: j["posted_at"] or "", reverse=True)
