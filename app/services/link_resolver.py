"""
Resolve aggregator interstitial links to the real apply URL.

Adzuna, Jooble and Careerjet don't hand out the employer's link — they hand out
a link to *their own* redirect page (`adzuna.com/land/ad/123`, `jooble.org/away/...`).
That costs us twice: the user has to click through an ad page to apply, and ATS
discovery never sees the Greenhouse/Lever/Workday URL hiding behind it, so we
never learn the company's board.

Following those redirects once, at fetch time, fixes both. The final URL is
stored on the job as `apply_url`, and the landing page's HTML is handed back to
the caller so ATS slugs and career-portal hosts can be mined out of it.
"""

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import unquote, urlparse

import httpx

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

DEFAULT_TIMEOUT = 12
DEFAULT_WORKERS = 8

# URL shapes that are known to be a redirect/interstitial rather than the real
# posting. Matched against the full URL, case-insensitively.
_INTERSTITIAL_PATTERNS = [
    re.compile(p, re.I) for p in (
        r"adzuna\.[a-z.]+/(?:land|jobs)/ad/",
        r"adzuna\.[a-z.]+/details/",
        r"jooble\.org/(?:away|jdp)/",
        r"careerjet\.[a-z.]+/jobad/",
        r"\.careerjet\.[a-z.]+/",
        r"indeed\.com/(?:rc/clk|pagead/clk|applystart)",
        r"/redirect\?",
        r"/out\?url=",
    )
]

# Hosts that are still an aggregator after redirects — landing on one of these
# means we did not reach the employer, so don't advertise it as an apply URL.
_AGGREGATOR_HOSTS = frozenset({
    "adzuna.com", "www.adzuna.com", "jooble.org", "www.jooble.org",
    "careerjet.com", "www.careerjet.com", "indeed.com", "www.indeed.com",
    "glassdoor.com", "www.glassdoor.com", "ziprecruiter.com",
    "www.ziprecruiter.com", "linkedin.com", "www.linkedin.com",
    "simplyhired.com", "www.simplyhired.com", "talent.com", "www.talent.com",
    "neuvoo.com", "jobs2careers.com", "monster.com", "www.monster.com",
})

# Fallbacks for landing pages that redirect with markup instead of a 3xx.
_META_REFRESH_RE = re.compile(
    r'<meta[^>]+http-equiv=["\']?refresh["\']?[^>]+content=["\'][^"\']*url=([^"\'>]+)',
    re.I,
)
_JS_REDIRECT_RE = re.compile(
    r'(?:window\.)?location(?:\.href)?\s*=\s*["\']([^"\']+)["\']', re.I
)
_CANONICAL_APPLY_RE = re.compile(
    r'<a[^>]+(?:id|class)="[^"]*(?:apply|redirect)[^"]*"[^>]*href="([^"]+)"', re.I
)


@dataclass
class ResolvedLink:
    """Outcome of following one interstitial link."""
    original_url: str
    final_url: str | None = None
    html: str = ""
    error: str | None = None

    @property
    def resolved(self) -> bool:
        return bool(self.final_url) and self.final_url != self.original_url


@dataclass
class ResolveStats:
    attempted: int = 0
    resolved: int = 0
    failed: int = 0
    skipped_budget: int = 0
    landing_html: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "attempted": self.attempted,
            "resolved": self.resolved,
            "failed": self.failed,
            "skipped_budget": self.skipped_budget,
        }


def is_interstitial(url: str) -> bool:
    """True when the URL points at an aggregator redirect page, not the posting."""
    if not url:
        return False
    return any(pattern.search(url) for pattern in _INTERSTITIAL_PATTERNS)


def is_aggregator(url: str) -> bool:
    """True when the URL's host is a job board rather than an employer/ATS site."""
    host = (urlparse(url).hostname or "").lower()
    return host in _AGGREGATOR_HOSTS


def _absolutize(candidate: str, base: str) -> str:
    candidate = unquote(candidate.strip())
    if candidate.startswith("//"):
        return f"{urlparse(base).scheme or 'https'}:{candidate}"
    if candidate.startswith("/"):
        parsed = urlparse(base)
        return f"{parsed.scheme}://{parsed.netloc}{candidate}"
    return candidate


def _redirect_from_html(html: str, base_url: str) -> str | None:
    """Some landing pages bounce via meta-refresh or JS instead of a 3xx."""
    for pattern in (_META_REFRESH_RE, _JS_REDIRECT_RE, _CANONICAL_APPLY_RE):
        match = pattern.search(html)
        if not match:
            continue
        candidate = _absolutize(match.group(1), base_url)
        if candidate.startswith("http") and candidate != base_url:
            return candidate
    return None


def resolve_url(url: str, client: httpx.Client) -> ResolvedLink:
    """Follow one interstitial to the page it actually points at."""
    try:
        resp = client.get(url)
    except Exception as exc:
        return ResolvedLink(original_url=url, error=str(exc))

    final_url = str(resp.url)
    html = resp.text if "html" in resp.headers.get("content-type", "").lower() else ""

    # Still on the aggregator after redirects: the real link is in the markup.
    if html and (final_url == url or is_interstitial(final_url)):
        hop = _redirect_from_html(html, final_url)
        if hop:
            try:
                hop_resp = client.get(hop)
                final_url = str(hop_resp.url)
                if "html" in hop_resp.headers.get("content-type", "").lower():
                    html = hop_resp.text
            except Exception as exc:
                logger.debug("second hop failed for %s: %s", hop, exc)
                final_url = hop

    return ResolvedLink(original_url=url, final_url=final_url, html=html)


def resolve_jobs(
    jobs: list[dict],
    max_links: int,
    workers: int = DEFAULT_WORKERS,
    timeout: int = DEFAULT_TIMEOUT,
) -> ResolveStats:
    """
    Resolve every interstitial URL among `jobs`, in place.

    Each resolved job gains an `apply_url` (the employer/ATS link, when we got
    off the aggregator). Landing-page HTML is returned in the stats rather than
    attached to the jobs — it's only needed for slug mining and would otherwise
    be persisted as job text.
    """
    stats = ResolveStats()
    candidates = [job for job in jobs if is_interstitial(job.get("url") or "")]
    if not candidates:
        return stats

    # One resolution per distinct URL, however many jobs share it.
    by_url: dict[str, list[dict]] = {}
    for job in candidates:
        by_url.setdefault(job["url"], []).append(job)

    urls = list(by_url)
    if len(urls) > max_links:
        stats.skipped_budget = len(urls) - max_links
        urls = urls[:max_links]
    stats.attempted = len(urls)

    with httpx.Client(
        headers=_HEADERS, timeout=timeout, follow_redirects=True, max_redirects=10
    ) as client:
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(urls)))) as pool:
            results = list(pool.map(lambda u: resolve_url(u, client), urls))

    for result in results:
        if result.error or not result.final_url:
            stats.failed += 1
            continue
        if result.html:
            stats.landing_html[result.original_url] = result.html
        if not result.resolved:
            continue
        stats.resolved += 1
        for job in by_url[result.original_url]:
            # Landing back on another aggregator isn't an apply link, but the
            # page may still carry the ATS URL, so the HTML is kept either way.
            if not is_aggregator(result.final_url):
                job["apply_url"] = result.final_url

    logger.info(
        "link_resolver: %d/%d interstitials resolved (%d failed, %d over budget)",
        stats.resolved, stats.attempted, stats.failed, stats.skipped_budget,
    )
    return stats
