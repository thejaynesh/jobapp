import logging
import re
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import httpx

from app.services.sources.base import (
    board_workers,
    cycle_cfg,
    fetch_boards_concurrently,
    parse_experience_level,
    rank_by_title,
    BoardResult,
    board_cursor,
)

logger = logging.getLogger(__name__)

# A Workday board is identified by a "tenant:host:site" triple, e.g.
# "nvidia:wd5:NVIDIAExternalCareerSite" →
#   https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/jobs
_LIST_URL = "https://{tenant}.{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
_DETAIL_URL = "https://{tenant}.{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{path}"

_PAGE_SIZE = 20
_MAX_DETAILS_PER_TENANT = 20  # each description is one extra request
_MAX_QUERIES_PER_TENANT = 10
_MAX_LIST_REQUESTS_PER_TENANT = 20
_MAX_PAGES_PER_QUERY = 3

# Rate limits, per Workday cluster. Tenants share hosts — most of ours sit on
# wd1 and wd5 — so a 429 to one tenant is the cluster saying "slow down" to
# every tenant on it. career-radar (haoawake/career-radar) keys its cooldown
# the same way. After a 429 the cluster rests for the server's Retry-After, or
# the settings page's cooldown, and every request to it waits that out; the
# other clusters carry on. A cluster refusing again and again is left for the
# rest of the cycle rather than slept on.
_MAX_COOLDOWN = 300.0
_MAX_TRIPS_PER_CYCLE = 3
_now = time.monotonic
_sleep = time.sleep


class _ClusterGate:
    def __init__(self):
        self._lock = threading.Lock()
        self._until: dict[str, float] = {}
        self._trips: dict[str, int] = {}
        # This cycle's default rest, set by `fetch` in the calling thread so the
        # board workers, which do not see the cycle's settings, have it too.
        self.cooldown = 0.0

    def new_cycle(self, cooldown: float) -> None:
        with self._lock:
            self._trips.clear()
            self.cooldown = cooldown

    def trip(self, cluster: str, seconds: float) -> None:
        with self._lock:
            until = _now() + max(0.0, min(seconds, _MAX_COOLDOWN))
            self._until[cluster] = max(self._until.get(cluster, 0.0), until)
            self._trips[cluster] = self._trips.get(cluster, 0) + 1
        logger.warning("Workday: %s rate-limited us; resting it %.0fs", cluster, seconds)

    def open(self, cluster: str) -> bool:
        """Wait out the cluster's rest; False once it has refused too often."""
        with self._lock:
            if self._trips.get(cluster, 0) >= _MAX_TRIPS_PER_CYCLE:
                return False
            delay = self._until.get(cluster, 0.0) - _now()
        if delay > 0:
            _sleep(delay)
        return True


_GATE = _ClusterGate()


def _retry_after(resp, default: float) -> float:
    value = (resp.headers.get("Retry-After") or "").strip()
    if value.isdigit():
        return float(value)
    try:
        return max(0.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return default


def _cooldown_setting() -> float:
    try:
        return max(0.0, float(getattr(cycle_cfg(), "WORKDAY_RATE_LIMIT_COOLDOWN", 60)))
    except (TypeError, ValueError):
        return 60.0


_STRIP_TAGS = re.compile(r"<[^>]+>")
_RELATIVE_POSTED = re.compile(r"posted\s+(today|yesterday|(\d+)\+?\s+days?\s+ago)", re.I)


_SITEMAP_SITE = re.compile(r"^\s*Sitemap:\s*https?://[^/]+/([A-Za-z0-9_-]+)/siteMap\.xml",
                           re.I | re.M)


def sites_for(tenant: str, host: str) -> list[str]:
    """
    Every external career site a Workday tenant publishes, from its robots.txt.

    A tenant is often more than one site, and the one discovery found first is
    rarely the one a new graduate wants: Salesforce keeps
    `Futureforce_NewGradRoles` and `Futureforce_Internships` apart from
    `External_Career_Site`, Rockwell has `…-Early-Careers`. Each site's sitemap
    is listed in the tenant's robots.txt, which is where they are read from.
    """
    resp = httpx.get(f"https://{tenant}.{host}.myworkdayjobs.com/robots.txt",
                     timeout=15, follow_redirects=True)
    resp.raise_for_status()
    return list(dict.fromkeys(_SITEMAP_SITE.findall(resp.text or "")))


def parse_tenant_spec(spec: str) -> tuple[str, str, str] | None:
    parts = [p.strip() for p in spec.split(":")]
    if len(parts) == 3 and all(parts):
        return parts[0], parts[1], parts[2]
    logger.warning("Workday: invalid tenant spec %r (want tenant:host:site)", spec)
    return None


def _posted_at_from_text(text: str) -> str | None:
    """Listings carry relative text ('Posted Today', 'Posted 7 Days Ago')."""
    m = _RELATIVE_POSTED.search(text or "")
    if not m:
        return None
    token = m.group(1).lower()
    if token == "today":
        days = 0
    elif token == "yesterday":
        days = 1
    else:
        days = int(m.group(2))
        if "+" in m.group(0):
            days += 1  # "30+ days" — at least that old
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _fetch_detail(tenant: str, host: str, site: str, path: str) -> dict:
    cooldown = _GATE.cooldown
    if cooldown and not _GATE.open(host):
        return {}
    try:
        resp = httpx.get(
            _DETAIL_URL.format(tenant=tenant, host=host, site=site, path=path),
            headers={"Accept": "application/json"},
            timeout=15,
        )
        if resp.status_code == 429 and cooldown:
            # The description is left for enrichment rather than retried here:
            # listings matter more than a detail, and share the same budget.
            _GATE.trip(host, _retry_after(resp, cooldown))
        resp.raise_for_status()
        return resp.json().get("jobPostingInfo") or {}
    except Exception as exc:
        logger.warning("Workday detail error (%s%s): %s", tenant, path, exc)
        return {}


def _detail_paths(postings: list[dict], queries: list[str], budget: int) -> set[str]:
    """
    Which postings get their one detail request: the titles matching wants,
    first (`base.rank_by_title`).

    Workday's search is loose — "Software Engineer" brings back Sales Engineer
    and Engineering Manager — and the budget used to go in listing order, so
    it went to postings the title gate discards minutes later while the ones
    it keeps arrived without a description. Nothing is dropped, only described
    later; enrichment reads Workday's detail API for whatever survives
    matching.
    """
    ordered = rank_by_title(postings, queries, lambda p: p.get("title"))
    return {p["externalPath"] for p in ordered[:max(0, budget)]}


def fetch(tenant_specs: list[str], queries: list[str]) -> list[dict]:
    """
    Fetch bounded pages from Workday sites, trying each role before deeper pages.
    Deduped by posting path; full descriptions come from capped per-job detail
    calls (which also carry the real posted date and public URL).
    """
    cooldown = _cooldown_setting()
    _GATE.new_cycle(cooldown)
    cfg = cycle_cfg()
    detail_budget = max(0, int(getattr(cfg, "WORKDAY_MAX_DETAILS_PER_BOARD", _MAX_DETAILS_PER_TENANT)))
    query_budget = max(1, int(getattr(cfg, "WORKDAY_MAX_QUERIES_PER_BOARD", _MAX_QUERIES_PER_TENANT)))
    request_budget = max(1, int(getattr(cfg, "WORKDAY_MAX_LIST_REQUESTS_PER_BOARD", _MAX_LIST_REQUESTS_PER_TENANT)))
    page_budget = max(1, int(getattr(cfg, "WORKDAY_MAX_PAGES_PER_QUERY", _MAX_PAGES_PER_QUERY)))

    def _fetch_one(spec: str) -> BoardResult:
        parsed = parse_tenant_spec(spec)
        if not parsed:
            return BoardResult(error="invalid Workday board specification", error_category="configuration")
        tenant, host, site = parsed
        retried: set[tuple[str, int]] = set()

        jobs: list[dict] = []
        seen_paths: set[str] = set()
        postings: list[dict] = []
        requested = [q for q in dict.fromkeys(queries) if q.strip()]
        saved = board_cursor("workday", spec)
        entries = saved.get("pending") if saved.get("queries") == requested else None
        pending = deque((str(q), int(offset)) for q, offset in entries) if entries else deque((q, 0) for q in requested)
        deferred = []
        admitted = list(dict.fromkeys(q for q, _ in pending))[:query_budget]
        deferred.extend((q, offset) for q, offset in pending if q not in admitted)
        pending = deque((q, offset) for q, offset in pending if q in admitted)
        query_paths: dict[str, set[str]] = {}
        query_pages: dict[str, int] = {}
        errors = []
        requests = 0
        while pending and requests < request_budget:
            if cooldown and not _GATE.open(host):
                logger.warning("Workday: %s keeps refusing; leaving %s for this cycle",
                               host, spec)
                errors.append("Workday cluster rate limit")
                break
            query, offset = pending.popleft()
            requests += 1
            try:
                resp = httpx.post(
                    _LIST_URL.format(tenant=tenant, host=host, site=site),
                    json={"limit": _PAGE_SIZE, "offset": offset,
                          "searchText": query, "appliedFacets": {}},
                    timeout=15,
                )
                if resp.status_code == 429 and cooldown:
                    _GATE.trip(host, _retry_after(resp, cooldown))
                    # Asked again once the cluster has rested.
                    if (query, offset) not in retried:
                        retried.add((query, offset))
                        pending.appendleft((query, offset))
                    else:
                        deferred.append((query, offset))
                        errors.append("Workday cluster rate limit")
                    continue
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                logger.error("Workday fetch error (%s / %r): %s", spec, query, exc)
                deferred.append((query, offset))
                errors.append(str(exc))
                continue
            rows = data.get("jobPostings") or []
            paths = {item.get("externalPath") for item in rows if item.get("externalPath")}
            previous = query_paths.setdefault(query, set())
            has_new = bool(paths - previous)
            previous.update(paths)
            for item in rows:
                path = item.get("externalPath") or ""
                if path and path not in seen_paths:
                    seen_paths.add(path)
                    postings.append(item)
            next_offset = offset + len(rows)
            try:
                total = int(data.get("total"))
            except (TypeError, ValueError):
                total = next_offset + 1
            query_pages[query] = query_pages.get(query, 0) + 1
            if has_new and len(rows) == _PAGE_SIZE and next_offset < total:
                if query_pages[query] < page_budget:
                    pending.append((query, next_offset))
                else:
                    deferred.append((query, next_offset))

        described = _detail_paths(postings, queries, detail_budget)
        for item in postings:
            path = item["externalPath"]
            title = (item.get("title") or "").strip()

            detail: dict = {}
            if path in described:
                detail = _fetch_detail(tenant, host, site, path)

            description = _STRIP_TAGS.sub(" ", detail.get("jobDescription") or "").strip()
            location = (detail.get("location") or item.get("locationsText") or "").strip()
            url = detail.get("externalUrl") or (
                f"https://{tenant}.{host}.myworkdayjobs.com/{site}{path}"
            )
            posted_at = detail.get("startDate") or _posted_at_from_text(item.get("postedOn") or "")
            remote_type = (item.get("remoteType") or "").lower()
            is_remote = "remote" in remote_type or "remote" in location.lower()

            jobs.append({
                "source": "workday",
                "source_job_id": f"{tenant}{path}",
                "title": title,
                "company": tenant,
                "location": location,
                "is_remote": is_remote,
                "url": url,
                "description": description,
                "experience_level": parse_experience_level(title, description),
                "posted_at": posted_at,
            })
        remaining = list(dict.fromkeys([*pending, *deferred]))
        return BoardResult(jobs=jobs, complete=not remaining and not errors,
                           cursor={"queries": requested, "pending": remaining} if remaining else None,
                           error="; ".join(errors[:3]) or None,
                           error_category="request_failed" if errors else None)

    return fetch_boards_concurrently(tenant_specs, _fetch_one, "Workday", board_workers())
