"""
ATS company-slug auto-discovery.

Jobs fetched from aggregators (LinkedIn, JSearch, HN, The Muse, ...) frequently
link to the company's own ATS board (boards.greenhouse.io/<slug>, jobs.lever.co/
<slug>, ...). Those boards are the best sources we have — full descriptions,
direct apply links, no scraping — so every fetch cycle scans the fetched jobs'
URLs and descriptions for ATS links, persists the discovered slugs on the
profile, and feeds them into the next cycle's direct board fetches.
"""

import logging
import re

logger = logging.getLogger(__name__)

MAX_SLUGS_PER_ATS = 25

ATS_PATTERNS: dict[str, re.Pattern] = {
    "greenhouse": re.compile(r"(?:boards|job-boards)\.greenhouse\.io/([A-Za-z0-9_-]{2,})", re.I),
    "lever": re.compile(r"jobs\.lever\.co/([A-Za-z0-9_-]{2,})", re.I),
    "ashby": re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.\-]{2,})", re.I),
    "smartrecruiters": re.compile(r"jobs\.smartrecruiters\.com/([A-Za-z0-9_-]{2,})", re.I),
    "workable": re.compile(r"apply\.workable\.com/(?:api/)?([A-Za-z0-9-]{2,})", re.I),
    "recruitee": re.compile(r"https?://([A-Za-z0-9-]{2,})\.recruitee\.com", re.I),
}

# Path segments and subdomains that match the patterns but aren't company slugs.
_SLUG_BLOCKLIST = frozenset({
    "embed", "api", "static", "assets", "www", "app", "jobs", "j", "widget",
    "careers", "share", "hire", "docs", "help", "blog",
})


def _extract_slugs(text: str) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    if not text:
        return found
    for ats, pattern in ATS_PATTERNS.items():
        for match in pattern.finditer(text):
            slug = match.group(1).lower().rstrip(".")
            if slug and slug not in _SLUG_BLOCKLIST:
                found.setdefault(ats, set()).add(slug)
    return found


def discover_ats_slugs(raw_jobs: list[dict], existing: dict | None = None) -> dict[str, list[str]]:
    """
    Scan fetched jobs for ATS board links and merge newly found company slugs
    into the existing mapping. Returns {"greenhouse": [...], "lever": [...], ...}
    with per-ATS caps (newest discoveries are dropped first when full).
    """
    merged: dict[str, list[str]] = {
        ats: list(slugs or []) for ats, slugs in (existing or {}).items()
        if ats in ATS_PATTERNS
    }

    new_count = 0
    for job in raw_jobs:
        # A job already fetched from an ATS shouldn't rediscover itself.
        if job.get("source") in ATS_PATTERNS:
            continue
        text = f"{job.get('url') or ''}\n{job.get('description') or ''}"
        for ats, slugs in _extract_slugs(text).items():
            bucket = merged.setdefault(ats, [])
            for slug in sorted(slugs):
                if slug not in bucket and len(bucket) < MAX_SLUGS_PER_ATS:
                    bucket.append(slug)
                    new_count += 1

    if new_count:
        logger.info(
            "ats_discovery: %d new company slugs — %s",
            new_count,
            {ats: len(slugs) for ats, slugs in merged.items()},
        )
    return merged


def merged_slugs(configured_csv: str, discovered: dict | None, ats: str) -> list[str]:
    """Configured (env) slugs first, then discovered ones, deduplicated."""
    result: list[str] = []
    seen: set[str] = set()
    for slug in [s.strip() for s in (configured_csv or "").split(",")]:
        if slug and slug.lower() not in seen:
            seen.add(slug.lower())
            result.append(slug)
    for slug in (discovered or {}).get(ats, []) or []:
        if slug and slug.lower() not in seen:
            seen.add(slug.lower())
            result.append(slug)
    return result
