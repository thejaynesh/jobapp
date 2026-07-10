import logging
import re
from datetime import datetime, timedelta, timezone

import httpx

from app.services.sources.base import parse_experience_level

logger = logging.getLogger(__name__)

# A Workday board is identified by a "tenant:host:site" triple, e.g.
# "nvidia:wd5:NVIDIAExternalCareerSite" →
#   https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/jobs
_LIST_URL = "https://{tenant}.{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
_DETAIL_URL = "https://{tenant}.{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{path}"

_PAGE_SIZE = 20
_MAX_DETAILS_PER_TENANT = 20  # each description is one extra request
_MAX_QUERIES_PER_TENANT = 5   # one search POST per query per tenant

_STRIP_TAGS = re.compile(r"<[^>]+>")
_RELATIVE_POSTED = re.compile(r"posted\s+(today|yesterday|(\d+)\+?\s+days?\s+ago)", re.I)


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
    try:
        resp = httpx.get(
            _DETAIL_URL.format(tenant=tenant, host=host, site=site, path=path),
            headers={"Accept": "application/json"},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("jobPostingInfo") or {}
    except Exception as exc:
        logger.warning("Workday detail error (%s%s): %s", tenant, path, exc)
        return {}


def fetch(tenant_specs: list[str], queries: list[str]) -> list[dict]:
    """
    Fetch jobs from Workday-hosted career sites. One search per (tenant, query),
    deduped by posting path; full descriptions come from capped per-job detail
    calls (which also carry the real posted date and public URL).
    """
    jobs: list[dict] = []
    for spec in tenant_specs:
        parsed = parse_tenant_spec(spec)
        if not parsed:
            continue
        tenant, host, site = parsed

        seen_paths: set[str] = set()
        postings: list[dict] = []
        for query in queries[:_MAX_QUERIES_PER_TENANT]:
            try:
                resp = httpx.post(
                    _LIST_URL.format(tenant=tenant, host=host, site=site),
                    json={"limit": _PAGE_SIZE, "offset": 0,
                          "searchText": query, "appliedFacets": {}},
                    timeout=15,
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                logger.error("Workday fetch error (%s / %r): %s", spec, query, exc)
                continue
            for item in data.get("jobPostings", []):
                path = item.get("externalPath") or ""
                if path and path not in seen_paths:
                    seen_paths.add(path)
                    postings.append(item)

        details_fetched = 0
        for item in postings:
            path = item["externalPath"]
            title = (item.get("title") or "").strip()

            detail: dict = {}
            if details_fetched < _MAX_DETAILS_PER_TENANT:
                detail = _fetch_detail(tenant, host, site, path)
                details_fetched += 1

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
    logger.info("Workday: %d jobs across %d tenants", len(jobs), len(tenant_specs))
    return jobs
