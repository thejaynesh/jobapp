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

import httpx

logger = logging.getLogger(__name__)

# Default cap on auto-discovered slugs per ATS. Cheap boards (one request per
# company) can carry many; per-company-expensive ATSes are capped lower below.
MAX_SLUGS_PER_ATS = 100
DISCOVERY_CAPS = {
    "workday": 15,          # searches × per-job detail calls per tenant
    "smartrecruiters": 30,  # per-posting detail calls per company
    "bamboohr": 30,
    "icims": 25,
    "teamtailor": 50,
    "jobvite": 50,
}


def _discovery_cap(ats: str) -> int:
    return DISCOVERY_CAPS.get(ats, MAX_SLUGS_PER_ATS)

# Several shapes per ATS: the public board URL people link to, the embed widget
# a company drops into its own careers page, and the API endpoint that widget
# calls. Careers pages very often only ever reveal the latter two.
ATS_PATTERNS: dict[str, list[re.Pattern]] = {
    "greenhouse": [
        # Embed widget: boards.greenhouse.io/embed/job_board?for=<slug>
        re.compile(r"greenhouse\.io/embed/job_board[^\"'\s]*[?&]for=([A-Za-z0-9_-]{2,})", re.I),
        re.compile(r"greenhouse\.io/(?:v1/)?boards/([A-Za-z0-9_-]{2,})", re.I),
        re.compile(r"(?:boards|job-boards)\.greenhouse\.io/([A-Za-z0-9_-]{2,})", re.I),
    ],
    "lever": [
        re.compile(r"jobs\.lever\.co/([A-Za-z0-9_-]{2,})", re.I),
        re.compile(r"api\.lever\.co/v0/postings/([A-Za-z0-9_-]{2,})", re.I),
    ],
    "ashby": [
        re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.\-]{2,})", re.I),
        re.compile(r"ashbyhq\.com/posting-api/job-board/([A-Za-z0-9_.\-]{2,})", re.I),
    ],
    "smartrecruiters": [
        re.compile(r"jobs\.smartrecruiters\.com/([A-Za-z0-9_-]{2,})", re.I),
        re.compile(r"api\.smartrecruiters\.com/v1/companies/([A-Za-z0-9_-]{2,})", re.I),
    ],
    "workable": [
        re.compile(r"apply\.workable\.com/(?:api/)?([A-Za-z0-9-]{2,})", re.I),
    ],
    "recruitee": [
        re.compile(r"https?://([A-Za-z0-9-]{2,})\.recruitee\.com", re.I),
    ],
    "icims": [
        # Both host shapes in the wild, normalized to the bare company slug.
        # The second pattern refuses the `careers-` prefix explicitly: without
        # that it also matches `careers-globex.icims.com` and registers
        # "careers-globex" as a second, duplicate board for the same company.
        re.compile(r"https?://careers-([A-Za-z0-9-]{2,})\.icims\.com", re.I),
        re.compile(r"https?://(?!careers-)([A-Za-z0-9-]{2,})\.icims\.com", re.I),
    ],
    "bamboohr": [
        re.compile(r"https?://([A-Za-z0-9-]{2,})\.bamboohr\.com", re.I),
    ],
    "teamtailor": [
        re.compile(r"https?://([A-Za-z0-9-]{2,})\.teamtailor\.com", re.I),
    ],
    "jobvite": [
        # jobs.jobvite.com/<slug> only. click.jobvite.com is the click tracker
        # (see link_resolver._TRACKER_DOMAINS), and reading a slug out of one
        # would register the tracker itself as a company board.
        re.compile(r"jobs\.jobvite\.com/(?:careers/)?([A-Za-z0-9_-]{2,})", re.I),
    ],
    "personio": [
        re.compile(r"https?://([A-Za-z0-9-]{2,})\.jobs\.personio\.(?:de|com)", re.I),
        re.compile(r"https?://([A-Za-z0-9-]{2,})\.jobs\.personio-int\.com", re.I),
    ],
}

# Workday boards need a tenant:host:site triple, extracted from URLs like
# https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/...
_WORKDAY_RE = re.compile(
    r"https?://([a-z0-9-]{2,})\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]{2,})",
    re.I,
)

# All ATS kinds we can fetch directly (patterned single-slug ones plus workday).
ALL_ATS = frozenset(ATS_PATTERNS) | {"workday"}

# Path segments and subdomains that match the patterns but aren't company slugs.
_SLUG_BLOCKLIST = frozenset({
    "embed", "api", "static", "assets", "www", "app", "jobs", "j", "widget",
    "careers", "share", "hire", "docs", "help", "blog", "wday", "job", "login",
})


def _extract_slugs(text: str) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    if not text:
        return found
    for ats, patterns in ATS_PATTERNS.items():
        for pattern in patterns:
            for match in pattern.finditer(text):
                slug = match.group(1).lower().rstrip(".")
                if slug and slug not in _SLUG_BLOCKLIST:
                    found.setdefault(ats, set()).add(slug)
    for match in _WORKDAY_RE.finditer(text):
        tenant, host, site = match.group(1).lower(), match.group(2).lower(), match.group(3)
        if site.lower() not in _SLUG_BLOCKLIST:
            found.setdefault("workday", set()).add(f"{tenant}:{host}:{site}")
    return found


def extract_slugs(text: str) -> dict[str, set[str]]:
    """Public wrapper: every ATS company slug referenced anywhere in `text`."""
    return _extract_slugs(text)


def discover_from_jobs(raw_jobs: list[dict]) -> dict[str, set[str]]:
    """
    ATS slugs referenced by a batch of fetched jobs, uncapped and unmerged —
    for callers that persist boards themselves (see services.company_boards).
    Scans each job's listing URL, its resolved apply URL, and its description.
    """
    found: dict[str, set[str]] = {}
    for job in raw_jobs:
        if job.get("source") in ALL_ATS:
            continue  # a job fetched from an ATS shouldn't rediscover itself
        text = "\n".join(filter(None, (
            job.get("url"), job.get("apply_url"), job.get("description"),
        )))
        for ats, slugs in _extract_slugs(text).items():
            found.setdefault(ats, set()).update(slugs)
    return found


def discover_ats_slugs(raw_jobs: list[dict], existing: dict | None = None) -> dict[str, list[str]]:
    """
    Scan fetched jobs for ATS board links and merge newly found company slugs
    into the existing mapping. Returns {"greenhouse": [...], "lever": [...], ...}
    with per-ATS caps (newest discoveries are dropped first when full).
    """
    merged: dict[str, list[str]] = {
        ats: list(slugs or []) for ats, slugs in (existing or {}).items()
        if ats in ALL_ATS
    }

    new_count = 0
    for job in raw_jobs:
        # A job already fetched from an ATS shouldn't rediscover itself.
        if job.get("source") in ALL_ATS:
            continue
        text = "\n".join(filter(None, (
            job.get("url"), job.get("apply_url"), job.get("description"),
        )))
        new_count += _merge_found(merged, _extract_slugs(text))

    if new_count:
        logger.info(
            "ats_discovery: %d new company slugs — %s",
            new_count,
            {ats: len(slugs) for ats, slugs in merged.items()},
        )
    return merged


def _merge_found(merged: dict[str, list[str]], found: dict[str, set[str]]) -> int:
    added = 0
    for ats, slugs in found.items():
        bucket = merged.setdefault(ats, [])
        cap = _discovery_cap(ats)
        for slug in sorted(slugs):
            if slug not in bucket and len(bucket) < cap:
                bucket.append(slug)
                added += 1
    return added


def harvest_slugs_from_lists(urls: list[str], existing: dict | None = None) -> dict[str, list[str]]:
    """
    Pull ATS company slugs out of community-maintained job lists (e.g. the
    SimplifyJobs new-grad README) — each list is one document full of direct
    apply links pointing at Greenhouse/Lever/Ashby/Workday/... boards.
    Returns the existing mapping merged with everything harvested (capped).
    """
    merged: dict[str, list[str]] = {
        ats: list(slugs or []) for ats, slugs in (existing or {}).items()
        if ats in ALL_ATS
    }
    for url in urls:
        try:
            resp = httpx.get(url, timeout=30, follow_redirects=True)
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("slug harvest failed for %s: %s", url, exc)
            continue
        added = _merge_found(merged, _extract_slugs(resp.text))
        logger.info("slug harvest: %d new slugs from %s", added, url)
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


# Which settings field carries each ATS's configured slugs.
ATS_CONFIG_FIELDS = {
    "greenhouse": "GREENHOUSE_COMPANY_SLUGS",
    "lever": "LEVER_COMPANY_SLUGS",
    "ashby": "ASHBY_COMPANY_SLUGS",
    "smartrecruiters": "SMARTRECRUITERS_COMPANY_SLUGS",
    "workable": "WORKABLE_COMPANY_SLUGS",
    "recruitee": "RECRUITEE_COMPANY_SLUGS",
    "workday": "WORKDAY_TENANTS",
    "icims": "ICIMS_COMPANY_SLUGS",
    "bamboohr": "BAMBOOHR_COMPANY_SLUGS",
    "teamtailor": "TEAMTAILOR_COMPANY_SLUGS",
    "jobvite": "JOBVITE_COMPANY_SLUGS",
    "personio": "PERSONIO_COMPANY_SLUGS",
}

# Bound per-cycle fetch time: cheap one-request-per-company boards can carry
# many slugs; per-company-expensive ATSes get tighter totals. Board fetches run
# concurrently (see sources.base.fetch_boards_concurrently), so these are far
# more generous than when each slug cost a serial round trip.
MAX_TOTAL_SLUGS_PER_ATS = 300
TOTAL_SLUG_CAPS = {
    "workday": 30,          # searches × per-job detail calls per tenant
    "smartrecruiters": 80,  # per-posting detail calls per company
    "bamboohr": 80,         # per-posting detail calls per company
    # Two host shapes tried per slug, and a full HTML page parsed each time.
    "icims": 60,
    "teamtailor": 120,
    "jobvite": 120,
}


def _total_cap(ats: str) -> int:
    from app.config import settings

    default = getattr(settings, "ATS_MAX_SLUGS_PER_ATS", MAX_TOTAL_SLUGS_PER_ATS)
    capped = TOTAL_SLUG_CAPS.get(ats)
    return min(capped, default) if capped is not None else default


def slug_caps() -> dict[str, int]:
    """The per-cycle slug budget for each ATS."""
    return {ats: _total_cap(ats) for ats in ATS_CONFIG_FIELDS}


def configured_ats_slugs(cfg) -> dict[str, list[str]]:
    """The raw configured slugs per ATS from settings."""
    result = {}
    for ats, field in ATS_CONFIG_FIELDS.items():
        result[ats] = [
            s.strip() for s in (getattr(cfg, field, "") or "").split(",") if s.strip()
        ]
    return result


def build_ats_slugs(
    cfg,
    discovered: dict | None = None,
    validated_configured: dict | None = None,
    registry: dict | None = None,
) -> dict[str, list[str]]:
    """
    Assemble the final slug list per ATS for one fetch cycle:
    configured (validated when available) → verified seed companies → registry
    boards (ranked by what they've actually yielded) → legacy discovered blob,
    deduplicated and capped.
    """
    from app.services.ats_seeds import SEED_ATS_SLUGS

    configured = (
        validated_configured if validated_configured is not None
        else configured_ats_slugs(cfg)
    )
    use_seeds = getattr(cfg, "ATS_SEED_COMPANIES", True)

    result: dict[str, list[str]] = {}
    for ats in ATS_CONFIG_FIELDS:
        cap = _total_cap(ats)
        seen: set[str] = set()
        merged: list[str] = []
        layers = [
            configured.get(ats, []),
            SEED_ATS_SLUGS.get(ats, []) if use_seeds else [],
            (registry or {}).get(ats, []) or [],
            (discovered or {}).get(ats, []) or [],
        ]
        for layer in layers:
            for slug in layer:
                if slug and slug.lower() not in seen and len(merged) < cap:
                    seen.add(slug.lower())
                    merged.append(slug)
        result[ats] = merged
    return result
