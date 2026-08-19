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
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote, urlparse

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

# How many times one link may bounce before we stop following it. Tracker
# chains are genuinely multi-hop (appcast hands off to recruitics, which hands
# off to the employer's ATS), which is why one hop was never enough.
DEFAULT_MAX_HOPS = 4

# Click trackers. They are not aggregators in the "another job board" sense —
# they are pure redirect middlemen — but they need to be in both lists below
# for the same reason: followed through, never stored.
#
# Storing one as an apply URL is how most "resolved" Adzuna links ended up
# pointing at click.appcast.io instead of an employer. The resolver followed
# HTTP redirects only; appcast bounces with JavaScript, so the tracker's own
# page was the last thing it saw and it recorded that as the destination.
_TRACKER_DOMAINS = frozenset({
    "appcast.io", "click.appcast.io",
    "recruitics.com", "jsv3.recruitics.com",
    "click.jobvite.com",
    "clickcast.jobs", "jobs2web.com", "trackmyjobs.co.uk",
})

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
        r"(?:^|//|\.)appcast\.io/",
        r"(?:^|//|\.)recruitics\.com/",
        r"click\.jobvite\.com/",
        r"(?:^|//|\.)clickcast\.jobs/",
        r"(?:^|//|\.)jobs2web\.com/",
        r"/redirect\?",
        r"/out\?url=",
    )
]

# Domains that are still an aggregator after redirects — landing on one of
# these means we did not reach the employer, so don't advertise it as an apply
# URL. Matched by suffix, so uk.indeed.com and m.linkedin.com count too.
#
# jobvite.com itself is deliberately absent: it is a real ATS whose job pages
# we want. Only its click-tracker subdomain is listed.
_AGGREGATOR_DOMAINS = frozenset({
    "adzuna.com", "jooble.org", "careerjet.com", "indeed.com",
    "glassdoor.com", "ziprecruiter.com", "linkedin.com", "simplyhired.com",
    "talent.com", "neuvoo.com", "jobs2careers.com", "monster.com",
}) | _TRACKER_DOMAINS

# Query parameters that carry the real destination. Trackers that never manage
# to redirect at all — the page 403s, the JavaScript needs a browser — often
# still spell out where they were going, and reading it costs nothing.
_REDIRECT_QUERY_KEYS = frozenset({
    "url", "u", "destination", "dest", "redirect", "redirect_url", "redirecturl",
    "target", "to", "link", "joburl", "job_url", "joburl", "rurl", "goto",
})

# Fallbacks for landing pages that redirect with markup instead of a 3xx.
_META_REFRESH_RE = re.compile(
    r'<meta[^>]+http-equiv=["\']?refresh["\']?[^>]+content=["\'][^"\']*url=([^"\'>]+)',
    re.I,
)
_JS_REDIRECT_RE = re.compile(
    r'(?:window\.)?location(?:\.href)?\s*=\s*["\']([^"\']+)["\']', re.I
)
# location.replace(...) / location.assign(...) — appcast's own bounce, and the
# reason its chains were never followed.
_JS_REPLACE_RE = re.compile(
    r'location\.(?:replace|assign)\s*\(\s*["\']([^"\']+)["\']', re.I
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
    # Followed the chain and still came out on an aggregator or a tracker. Its
    # own number because "resolved" hid it: a link that moved from Adzuna to
    # appcast counted as a success while pointing at nothing anyone can apply
    # through.
    stopped_at_aggregator: int = 0
    landing_html: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "attempted": self.attempted,
            "resolved": self.resolved,
            "failed": self.failed,
            "skipped_budget": self.skipped_budget,
            "stopped_at_aggregator": self.stopped_at_aggregator,
        }


def is_interstitial(url: str) -> bool:
    """True when the URL points at an aggregator redirect page, not the posting."""
    if not url:
        return False
    return any(pattern.search(url) for pattern in _INTERSTITIAL_PATTERNS)


def _host_matches(url: str, domains) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == domain or host.endswith(f".{domain}") for domain in domains)


def is_aggregator(url: str) -> bool:
    """True when the URL's host is a job board rather than an employer/ATS site."""
    return _host_matches(url, _AGGREGATOR_DOMAINS)


def is_tracker(url: str) -> bool:
    """True when the URL is a click-tracking middleman, not a destination."""
    return _host_matches(url, _TRACKER_DOMAINS)


def _target_from_query(url: str) -> str | None:
    """The destination a tracker spelled out in its own query string."""
    try:
        params = parse_qs(urlparse(url).query, keep_blank_values=False)
    except Exception:
        return None
    for key, values in params.items():
        if key.lower() not in _REDIRECT_QUERY_KEYS:
            continue
        for value in values:
            candidate = unquote((value or "").strip())
            if candidate.lower().startswith(("http://", "https://")):
                return candidate
    return None


class _HostLimiter:
    """
    At most N requests in flight per host, spaced by a small gap.

    Politeness is the only budget left now that the per-cycle cap is high: the
    thing worth avoiding is thirty simultaneous requests to one employer, not
    resolving a lot of links. Keyed by host, so a backlog that is 90% Adzuna
    paces itself while everything else runs at full speed.
    """

    def __init__(self, max_concurrent: int = 4, min_interval: float = 0.25):
        self._max = max(1, max_concurrent)
        self._interval = max(0.0, min_interval)
        self._guard = threading.Lock()
        self._slots: dict[str, threading.Semaphore] = {}
        self._pace: dict[str, threading.Lock] = {}
        self._last: dict[str, float] = {}

    @contextmanager
    def slot(self, url: str):
        host = (urlparse(url).hostname or "").lower()
        with self._guard:
            semaphore = self._slots.setdefault(host, threading.Semaphore(self._max))
            pace = self._pace.setdefault(host, threading.Lock())

        semaphore.acquire()
        try:
            if self._interval:
                with pace:
                    wait = self._interval - (time.monotonic() - self._last.get(host, 0.0))
                    if wait > 0:
                        time.sleep(wait)
                    self._last[host] = time.monotonic()
            yield
        finally:
            semaphore.release()


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
    for pattern in (
        _META_REFRESH_RE, _JS_REDIRECT_RE, _JS_REPLACE_RE, _CANONICAL_APPLY_RE,
    ):
        match = pattern.search(html)
        if not match:
            continue
        candidate = _absolutize(match.group(1), base_url)
        if candidate.startswith("http") and candidate != base_url:
            return candidate
    return None


def _next_hop(url: str, html: str) -> str | None:
    """Where this page is really trying to send us, markup first, then query."""
    return (_redirect_from_html(html, url) if html else None) or _target_from_query(url)


def resolve_url(
    url: str,
    client: httpx.Client,
    limiter: "_HostLimiter | None" = None,
    max_hops: int = DEFAULT_MAX_HOPS,
) -> ResolvedLink:
    """
    Follow one interstitial all the way to the page it actually points at.

    Chained rather than single-hop: an Adzuna link hands off to appcast, which
    hands off to recruitics, which hands off to the employer's Workday. Stopping
    after one hop is what stored `click.appcast.io/...` as thousands of jobs'
    "real apply URL".
    """
    def _get(target: str):
        if limiter is None:
            return client.get(target)
        with limiter.slot(target):
            return client.get(target)

    try:
        resp = _get(url)
    except Exception as exc:
        # A tracker that refuses us outright often still names its destination
        # in its own query string, and that beats giving up.
        spelled_out = _target_from_query(url)
        if spelled_out:
            return ResolvedLink(original_url=url, final_url=spelled_out)
        return ResolvedLink(original_url=url, error=str(exc))

    final_url = str(resp.url)
    html = resp.text if "html" in resp.headers.get("content-type", "").lower() else ""

    seen = {url, final_url}
    for _ in range(max_hops):
        # Off the middlemen and onto something real: stop here.
        if final_url != url and not is_interstitial(final_url):
            break
        hop = _next_hop(final_url, html)
        if not hop or hop in seen:
            break
        seen.add(hop)
        try:
            hop_resp = _get(hop)
        except Exception as exc:
            # The hop is still the better answer than the tracker we were on.
            logger.debug("hop to %s failed: %s", hop, exc)
            final_url, html = hop, ""
            break
        final_url = str(hop_resp.url)
        seen.add(final_url)
        html = (
            hop_resp.text
            if "html" in hop_resp.headers.get("content-type", "").lower()
            else ""
        )

    return ResolvedLink(original_url=url, final_url=final_url, html=html)


def resolve_urls(
    urls: list[str],
    workers: int = DEFAULT_WORKERS,
    timeout: int = DEFAULT_TIMEOUT,
    per_host: int = 4,
    host_delay: float = 0.25,
) -> list[ResolvedLink]:
    """Follow a batch of links, politely, and hand back what each became."""
    if not urls:
        return []
    limiter = _HostLimiter(max_concurrent=per_host, min_interval=host_delay)
    with httpx.Client(
        headers=_HEADERS, timeout=timeout, follow_redirects=True, max_redirects=10
    ) as client:
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(urls)))) as pool:
            return list(pool.map(lambda u: resolve_url(u, client, limiter), urls))


def resolve_jobs(
    jobs: list[dict],
    max_links: int,
    workers: int = DEFAULT_WORKERS,
    timeout: int = DEFAULT_TIMEOUT,
    per_host: int = 4,
    host_delay: float = 0.25,
) -> ResolveStats:
    """
    Resolve every interstitial URL among `jobs`, in place.

    Each resolved job gains an `apply_url` (the employer/ATS link, when we got
    off the aggregator). Landing-page HTML is returned in the stats keyed by the
    job's original URL: slug mining reads it, and so does enrichment, which
    would otherwise download the very page we just had in hand.
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

    results = resolve_urls(
        urls, workers=workers, timeout=timeout,
        per_host=per_host, host_delay=host_delay,
    )

    for result in results:
        if result.error or not result.final_url:
            stats.failed += 1
            continue
        if result.html:
            stats.landing_html[result.original_url] = result.html
        if not result.resolved:
            continue
        stats.resolved += 1
        landed_on_middleman = is_aggregator(result.final_url)
        if landed_on_middleman:
            stats.stopped_at_aggregator += 1
        for job in by_url[result.original_url]:
            # Landing back on another aggregator — or on a click tracker — is
            # not an apply link, but the page may still carry the ATS URL, so
            # the HTML is kept either way.
            if not landed_on_middleman:
                job["apply_url"] = result.final_url

    logger.info(
        "link_resolver: %d/%d interstitials resolved (%d failed, %d stopped on a "
        "middleman, %d over budget)",
        stats.resolved, stats.attempted, stats.failed,
        stats.stopped_at_aggregator, stats.skipped_budget,
    )
    return stats


# SQL patterns for apply URLs that are a tracker rather than an employer. The
# precise test is `is_tracker`; this only narrows the scan.
_TRACKER_SQL_HINTS = ("%appcast.io%", "%recruitics.com%", "%click.jobvite.com%",
                      "%clickcast.jobs%", "%jobs2web.com%")


def retarget_tracker_links(
    db, limit: int = 5000, workers: int = DEFAULT_WORKERS,
    per_host: int = 4, host_delay: float = 0.25,
) -> dict:
    """
    Repair the apply URLs that stopped at a click tracker.

    Resumes from the tracker rather than starting the chain again: it is the
    deepest point the old resolver reached, and now that JavaScript bounces are
    followed, one more request usually finishes the journey. Jobs whose tracker
    still leads nowhere have their apply URL cleared, which puts them back in
    front of `enqueue_unresolved_links` for the browser to try.
    """
    from sqlalchemy import or_

    from app.models.job import Job

    counts = {"examined": 0, "repaired": 0, "cleared": 0, "unchanged": 0}

    rows = (
        db.query(Job.apply_url)
        .filter(
            Job.apply_url.isnot(None),
            or_(*[Job.apply_url.ilike(hint) for hint in _TRACKER_SQL_HINTS]),
        )
        .distinct()
        .limit(limit)
        .all()
    )
    tracker_urls = [row[0] for row in rows if is_tracker(row[0] or "")]
    counts["examined"] = len(tracker_urls)
    if not tracker_urls:
        return counts

    results = resolve_urls(
        tracker_urls, workers=workers, per_host=per_host, host_delay=host_delay
    )

    for result in results:
        destination = result.final_url or ""
        reached_employer = (
            destination
            and destination != result.original_url
            and not is_aggregator(destination)
        )
        # A hand-typed apply link is never a tracker in the first place, so
        # this is belt and braces — but the branch below *clears* the column,
        # and quietly deleting something the user typed is the one outcome
        # here worth being certain about.
        query = db.query(Job).filter(
            Job.apply_url == result.original_url,
            ~Job.manual_fields.any("apply_url"),
        )
        if reached_employer:
            counts["repaired"] += query.update(
                {"apply_url": destination}, synchronize_session=False
            )
        else:
            # NULL rather than left in place: a tracker URL is not something
            # the user can apply through, and clearing it is what makes the
            # job eligible for browser resolution again.
            counts["cleared"] += query.update(
                {"apply_url": None}, synchronize_session=False
            )

    db.commit()
    logger.info(
        "link_resolver: %d tracker apply URLs examined — %d repaired, %d cleared",
        counts["examined"], counts["repaired"], counts["cleared"],
    )
    return counts
