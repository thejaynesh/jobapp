"""
Find the ATS board hiding behind a company's own careers site.

Plenty of apply links look like `careers.acme.com/job/123` or `acme.com/jobs` —
no ATS pattern to match, so slug discovery throws them away. But most of those
pages are a thin wrapper around Greenhouse/Lever/Ashby: an embed iframe, a
`boards.greenhouse.io/embed/job_board?for=acme` script, or a plain link to the
real board. Fetching the careers page and mining it converts an unrecognised
portal into a board we can poll directly every cycle.

When the markup gives nothing away, the registrable domain is a good slug guess
(`acme.com` → `acme`), so the likeliest ATSes get probed for it directly.

Results — including misses — are cached by host so a company is sniffed once,
not once per cycle.
"""

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
}

_TIMEOUT = 10
DEFAULT_WORKERS = 6

# Paths worth trying on a company domain, cheapest signal first.
_CAREER_PATHS = ("/careers", "/jobs")

# Guessing a slug from the domain is cheap for these three (one API probe each)
# and they cover the large majority of startup boards.
_GUESSABLE_ATS = ("greenhouse", "lever", "ashby")

# Subdomains that prefix a careers site rather than name the company.
_CAREER_SUBDOMAINS = frozenset({
    "careers", "career", "jobs", "job", "www", "apply", "boards", "work",
    "hiring", "talent", "recruiting", "join",
})

# Hosts that are never a single company's careers site.
_SKIP_HOSTS = frozenset({
    "github.com", "github.io", "google.com", "docs.google.com", "bit.ly",
    "lnkd.in", "twitter.com", "x.com", "facebook.com", "youtube.com",
    "medium.com", "notion.so", "notion.site", "airtable.com", "typeform.com",
    "wellfound.com", "angel.co", "builtin.com", "dice.com", "monster.com",
})

# Public-suffix-ish second levels, so "acme.co.uk" yields "acme" not "co".
_COMPOUND_TLDS = frozenset({"co", "com", "net", "org", "gov", "edu", "ac"})

# Re-sniff a host that yielded nothing only after this long.
_MISS_TTL_DAYS = 30


def company_host(url: str) -> str | None:
    """The host of a URL, when it plausibly belongs to a single company."""
    host = (urlparse(url).hostname or "").lower().strip(".")
    if not host or "." not in host:
        return None
    if host in _SKIP_HOSTS or any(host.endswith("." + s) for s in _SKIP_HOSTS):
        return None
    # Never sniff a known ATS host — those already have real slug patterns.
    if re.search(r"(greenhouse|lever|ashbyhq|smartrecruiters|workable|recruitee|myworkdayjobs)\.",
                 host):
        return None
    return host


def domain_slug(host: str) -> str | None:
    """`careers.acme.co.uk` → `acme`. Returns None when there's nothing to guess."""
    labels = [l for l in host.split(".") if l]
    while len(labels) > 2 and labels[0] in _CAREER_SUBDOMAINS:
        labels = labels[1:]
    if len(labels) < 2:
        return None
    # Drop the TLD, plus a compound second level ("co.uk", "com.au").
    core = labels[:-1]
    if len(core) > 1 and core[-1] in _COMPOUND_TLDS:
        core = core[:-1]
    candidate = core[-1] if core else None
    if not candidate or len(candidate) < 2 or candidate in _CAREER_SUBDOMAINS:
        return None
    return candidate


def _origin(host: str) -> str:
    return f"https://{host}"


def sniff_host(host: str, seed_html: str = "") -> dict[str, list[str]]:
    """
    Look for ATS boards belonging to `host`.

    `seed_html` is the already-fetched landing page, when we have one — mining it
    first often avoids any extra request at all.
    """
    from app.services.ats_discovery import extract_slugs

    found: dict[str, set[str]] = {}

    def _absorb(text: str) -> bool:
        for ats, slugs in extract_slugs(text).items():
            found.setdefault(ats, set()).update(slugs)
        return bool(found)

    if seed_html and _absorb(seed_html):
        return {ats: sorted(slugs) for ats, slugs in found.items()}

    with httpx.Client(headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True) as client:
        for path in _CAREER_PATHS:
            try:
                resp = client.get(_origin(host) + path)
            except Exception as exc:
                logger.debug("sniff %s%s failed: %s", host, path, exc)
                continue
            if resp.status_code != 200:
                continue
            if _absorb(resp.text):
                return {ats: sorted(slugs) for ats, slugs in found.items()}

    # Nothing embedded — try the domain name as a slug on the common boards.
    guess = domain_slug(host)
    if guess:
        from app.services.ats_validation import is_valid_slug
        for ats in _GUESSABLE_ATS:
            try:
                if is_valid_slug(ats, guess):
                    logger.info("ats_sniffer: %s → %s/%s (domain guess)", host, ats, guess)
                    return {ats: [guess]}
            except Exception as exc:
                logger.debug("sniff probe %s/%s failed: %s", ats, guess, exc)

    return {}


def _is_fresh(entry: dict) -> bool:
    """A cached miss goes stale eventually; a cached hit never needs redoing."""
    if entry.get("found"):
        return True
    try:
        checked = datetime.fromisoformat(entry.get("at", ""))
    except Exception:
        return False
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - checked < timedelta(days=_MISS_TTL_DAYS)


def sniff_hosts(
    hosts_html: dict[str, str],
    cache: dict | None = None,
    max_hosts: int = 40,
    workers: int = DEFAULT_WORKERS,
) -> tuple[dict[str, list[str]], dict, dict]:
    """
    Sniff each host in `hosts_html` (host → landing HTML, possibly empty).

    Returns (found_slugs_per_ats, updated_cache, per_host_found). The cache is
    keyed by host so repeat cycles cost nothing.
    """
    cache = {k: dict(v) for k, v in (cache or {}).items() if isinstance(v, dict)}

    pending = [h for h in hosts_html if not (h in cache and _is_fresh(cache[h]))]
    if len(pending) > max_hosts:
        logger.info("ats_sniffer: %d hosts queued, sniffing %d this cycle",
                    len(pending), max_hosts)
        pending = pending[:max_hosts]

    if pending:
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(pending)))) as pool:
            results = list(pool.map(
                lambda h: (h, _safe_sniff(h, hosts_html.get(h, ""))), pending
            ))
        now = datetime.now(timezone.utc).isoformat()
        for host, result in results:
            cache[host] = {"at": now, "found": result}

    merged: dict[str, list[str]] = {}
    per_host: dict[str, dict] = {}
    for host in hosts_html:
        entry = cache.get(host) or {}
        found = entry.get("found") or {}
        if not found:
            continue
        per_host[host] = found
        for ats, slugs in found.items():
            bucket = merged.setdefault(ats, [])
            for slug in slugs:
                if slug not in bucket:
                    bucket.append(slug)

    if merged:
        logger.info("ats_sniffer: boards found for %d career sites — %s",
                    len(per_host), {a: len(s) for a, s in merged.items()})
    return merged, cache, per_host


def _safe_sniff(host: str, html: str) -> dict[str, list[str]]:
    try:
        return sniff_host(host, html)
    except Exception as exc:
        logger.warning("ats_sniffer: %s failed: %s", host, exc)
        return {}
