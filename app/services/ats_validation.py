"""
ATS slug validation and auto-correction.

Configured slugs are often slightly wrong ("Stripe Inc" instead of "stripe").
A wrong slug silently contributes nothing — the board API just 404s — so each
fetch cycle validates the configured slugs against the ATS APIs, tries obvious
variants for the broken ones, and reports what it found. Results are cached on
the profile so each slug is probed at most once.
"""

import logging
import re
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = 10

# Suffixes that people include in company names but slugs never carry.
_NAME_SUFFIXES = (
    "inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation",
    "co", "company", "labs", "technologies", "technology", "software", "hq",
)


def _probe_greenhouse(slug: str) -> bool:
    r = httpx.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}", timeout=_TIMEOUT)
    return r.status_code == 200


def _probe_lever(slug: str) -> bool:
    r = httpx.get(f"https://api.lever.co/v0/postings/{slug}?limit=1&mode=json", timeout=_TIMEOUT)
    return r.status_code == 200 and isinstance(r.json(), list)


def _probe_ashby(slug: str) -> bool:
    r = httpx.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", timeout=_TIMEOUT)
    return r.status_code == 200


def _probe_smartrecruiters(slug: str) -> bool:
    r = httpx.get(
        f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=1",
        timeout=_TIMEOUT,
    )
    return r.status_code == 200


def _probe_workable(slug: str) -> bool:
    r = httpx.get(
        f"https://apply.workable.com/api/v1/widget/accounts/{slug}",
        timeout=_TIMEOUT, follow_redirects=True,
    )
    return r.status_code == 200


def _probe_recruitee(slug: str) -> bool:
    r = httpx.get(f"https://{slug}.recruitee.com/api/offers/", timeout=_TIMEOUT,
                  follow_redirects=True)
    return r.status_code == 200


def _probe_workday(spec: str) -> bool:
    from app.services.sources.workday import parse_tenant_spec
    parsed = parse_tenant_spec(spec)
    if not parsed:
        return False
    tenant, host, site = parsed
    r = httpx.post(
        f"https://{tenant}.{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs",
        json={"limit": 1, "offset": 0, "searchText": "", "appliedFacets": {}},
        timeout=_TIMEOUT,
    )
    return r.status_code == 200


def _probe_bamboohr(slug: str) -> bool:
    r = httpx.get(f"https://{slug}.bamboohr.com/careers/list", timeout=_TIMEOUT,
                  follow_redirects=True)
    return r.status_code == 200


def _probe_personio(slug: str) -> bool:
    r = httpx.get(f"https://{slug}.jobs.personio.de/xml", timeout=_TIMEOUT,
                  follow_redirects=True)
    return r.status_code == 200 and r.content.lstrip()[:1] == b"<"


def _probe_listing(url: str) -> bool:
    """
    A board we read through structured data is valid when it publishes some.

    A 200 alone is not enough: iCIMS, Teamtailor and Jobvite all serve a
    friendly "company not found" page with a 200, and treating that as a live
    board would put a nonexistent company in the registry to be polled forever.
    """
    from app.services.sources.base import LISTING_HEADERS

    r = httpx.get(url, headers=LISTING_HEADERS, timeout=_TIMEOUT,
                  follow_redirects=True)
    return r.status_code == 200 and "jobposting" in r.text.lower()


def _probe_icims(slug: str) -> bool:
    return any(
        _probe_listing(f"https://{host}/jobs/search?ss=1")
        for host in (f"{slug}.icims.com", f"careers-{slug}.icims.com")
    )


def _probe_teamtailor(slug: str) -> bool:
    return _probe_listing(f"https://{slug}.teamtailor.com/jobs")


def _probe_jobvite(slug: str) -> bool:
    return _probe_listing(f"https://jobs.jobvite.com/{slug}/search")


PROBES = {
    "greenhouse": _probe_greenhouse,
    "lever": _probe_lever,
    "ashby": _probe_ashby,
    "smartrecruiters": _probe_smartrecruiters,
    "workable": _probe_workable,
    "recruitee": _probe_recruitee,
    "workday": _probe_workday,
    "icims": _probe_icims,
    "bamboohr": _probe_bamboohr,
    "teamtailor": _probe_teamtailor,
    "jobvite": _probe_jobvite,
    "personio": _probe_personio,
}


def is_valid_slug(ats: str, slug: str) -> bool:
    probe = PROBES.get(ats)
    if probe is None:
        return True  # unknown ATS — don't block
    try:
        return probe(slug)
    except Exception as exc:
        logger.warning("slug probe error (%s/%s): %s", ats, slug, exc)
        return True  # network trouble ≠ bad slug; let the adapter try


# The board APIs that name their own company, and where the name lives. Used
# to catch a slug that resolves to a real board belonging to somebody else —
# `greenhouse/appcast` is a live board, just not the company it was filed under.
_NAME_ENDPOINTS = {
    "greenhouse": ("https://boards-api.greenhouse.io/v1/boards/{slug}", ("name",)),
    "workable": ("https://apply.workable.com/api/v1/widget/accounts/{slug}", ("name",)),
    "recruitee": ("https://{slug}.recruitee.com/api/offers/",
                  ("offers", 0, "company_name")),
    "bamboohr": ("https://{slug}.bamboohr.com/careers/list", ("companyName",)),
}


def _dig(data, path):
    for key in path:
        if isinstance(key, int):
            if not isinstance(data, list) or len(data) <= key:
                return None
            data = data[key]
        elif isinstance(data, dict):
            data = data.get(key)
        else:
            return None
        if data is None:
            return None
    return data if isinstance(data, str) else None


def board_company_name(ats: str, slug: str) -> str | None:
    """The company name the board's own API reports, when it reports one."""
    endpoint = _NAME_ENDPOINTS.get(ats)
    if not endpoint:
        return None
    url, path = endpoint
    try:
        resp = httpx.get(url.format(slug=slug), timeout=_TIMEOUT,
                         follow_redirects=True)
        if resp.status_code != 200:
            return None
        return _dig(resp.json(), path)
    except Exception:
        return None


class BoardProbe:
    """What one validation probe found."""

    __slots__ = ("exists", "company", "error")

    def __init__(self, exists: bool, company: str | None = None,
                 error: str | None = None):
        self.exists = exists
        self.company = company
        self.error = error


def probe_board(ats: str, slug: str) -> BoardProbe:
    """
    Ask an ATS whether a slug is a real board, and whose.

    A network failure reports `exists=True` on purpose: not being able to reach
    the ATS is not evidence that the company is fictional, and marking a real
    board dead because of one timeout is the more expensive mistake.
    """
    probe = PROBES.get(ats)
    if probe is None:
        return BoardProbe(True)
    try:
        exists = bool(probe(slug))
    except Exception as exc:
        return BoardProbe(True, error=f"probe failed: {exc}")
    if not exists:
        return BoardProbe(False, error=f"{ats} has no board for this slug")
    return BoardProbe(True, company=board_company_name(ats, slug))


def candidate_slugs(name: str) -> list[str]:
    """Plausible slug variants for a company name, most likely first."""
    base = name.strip().lower()
    words = re.findall(r"[a-z0-9]+", base)
    trimmed = list(words)
    while len(trimmed) > 1 and trimmed[-1] in _NAME_SUFFIXES:
        trimmed = trimmed[:-1]

    candidates = []
    for parts in (trimmed, words):
        candidates.append("".join(parts))
        candidates.append("-".join(parts))
    if trimmed:
        candidates.append(trimmed[0])

    seen: set[str] = set()
    unique = []
    for c in candidates:
        if c and len(c) >= 2 and c != base and c not in seen:
            seen.add(c)
            unique.append(c)
    return unique


def resolve_slug(ats: str, slug: str) -> str | None:
    """Return a working slug for `slug` (itself, a fixed variant, or None)."""
    if is_valid_slug(ats, slug):
        return slug
    for candidate in candidate_slugs(slug):
        if is_valid_slug(ats, candidate):
            logger.info("slug fix: %s/%r -> %r", ats, slug, candidate)
            return candidate
    return None


def validate_configured_slugs(
    configured: dict[str, list[str]], cache: dict | None
) -> tuple[dict[str, list[str]], dict, dict]:
    """
    Validate/fix the user-configured slugs per ATS.

    Returns (valid_slugs_per_ats, updated_cache, report).
    cache: {ats: {original: resolved_slug_or_None}} — persisted on the profile
    so each slug is probed at most once.
    report: {ats: {"fixed": {orig: fixed}, "invalid": [orig, ...]}}
    """
    cache = {k: dict(v or {}) for k, v in (cache or {}).items()}
    valid: dict[str, list[str]] = {}
    report: dict[str, dict] = {}

    for ats, slugs in configured.items():
        ats_cache = cache.setdefault(ats, {})
        kept: list[str] = []
        fixed: dict[str, str] = {}
        invalid: list[str] = []
        for slug in slugs:
            if slug in ats_cache:
                resolved = ats_cache[slug]
            else:
                resolved = resolve_slug(ats, slug)
                ats_cache[slug] = resolved
            if resolved is None:
                invalid.append(slug)
            else:
                kept.append(resolved)
                if resolved != slug:
                    fixed[slug] = resolved
        valid[ats] = kept
        if fixed or invalid:
            report[ats] = {"fixed": fixed, "invalid": invalid}
            if invalid:
                logger.warning("ats_validation: invalid %s slugs (no fix found): %s", ats, invalid)

    if report:
        report["checked_at"] = datetime.now(timezone.utc).isoformat()
    return valid, cache, report
