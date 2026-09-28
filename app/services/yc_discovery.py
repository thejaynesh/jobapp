"""
Company boards behind the websites of YC companies that are hiring.

yc-oss (github.com/yc-oss/api) republishes Y Combinator's company directory as
JSON; `companies/hiring.json` is the ~1,500 marked as hiring, each with its own
website. Most run a board on Greenhouse, Lever or Ashby that no posting we hold
has linked to, and the careers-site sniffer finds it from the website: of 80
sampled on 2026-09-28 it found a board for 26, 8 of them new to us.

On the hourly discovery tick, a batch of sites at a time (the settings page's
"YC sites looked behind per hour"), from a queue refilled weekly. The sniffer's
own cache is shared, so a site it has already looked behind costs nothing and a
miss is not retried for a month; every board found is probed before it is
polled, like any other.
"""

import copy
import logging
from datetime import datetime, timedelta, timezone

import httpx

logger = logging.getLogger(__name__)

HIRING_URL = "https://yc-oss.github.io/api/companies/hiring.json"
STATE_KEY = "yc_discovery"
REFRESH_DAYS = 7
_TIMEOUT = 60


def hiring_sites() -> list[list[str]]:
    """`[host, website, company]` for each hiring YC company with a website."""
    from app.services.ats_sniffer import company_host

    resp = httpx.get(HIRING_URL, timeout=_TIMEOUT, follow_redirects=True)
    resp.raise_for_status()
    rows = resp.json()
    sites: dict[str, list[str]] = {}
    for company in rows if isinstance(rows, list) else []:
        if not isinstance(company, dict) or company.get("isHiring") is False:
            continue
        website = str(company.get("website") or "").strip()
        host = company_host(website) if website.startswith("http") else None
        if host:
            sites.setdefault(host, [host, website, str(company.get("name") or "").strip()])
    return sorted(sites.values())


def _due(stamp: str | None) -> bool:
    if not stamp:
        return True
    try:
        then = datetime.fromisoformat(stamp)
    except ValueError:
        return True
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - then >= timedelta(days=REFRESH_DAYS)


def run(db, per_run: int) -> dict:
    """Look behind the next `per_run` sites in the queue; report what was found."""
    from app.models.profile import Profile
    from app.services import company_boards
    from app.services.ats_sniffer import sniff_hosts

    profile = db.query(Profile).first()
    if profile is None or per_run <= 0:
        return {"skipped": True}
    state = dict((profile.data or {}).get(STATE_KEY) or {})
    pending = [row for row in state.get("pending") or [] if isinstance(row, list) and len(row) == 3]
    report: dict = {}
    if not pending and _due(state.get("fetched_at")):
        pending = hiring_sites()
        state["fetched_at"] = datetime.now(timezone.utc).isoformat()
        report["queued"] = len(pending)
    batch, rest = pending[:per_run], pending[per_run:]

    found_boards = 0
    cache = None
    if batch:
        hosts = {host: "" for host, _, _ in batch}
        hints = {host: {"url": website, "company": company} for host, website, company in batch}
        _, cache, per_host = sniff_hosts(
            hosts, (profile.data or {}).get("ats_sniff_cache"),
            max_hosts=len(batch), hints=hints)
        names = {host: company for host, _, company in batch}
        for host, found in per_host.items():
            found_boards += company_boards.record_boards(
                db, found, origin="sniffed", company=names.get(host) or None,
                source_host=host, revive=False)

    # Re-read before writing: a fetch cycle may have written this blob since.
    db.refresh(profile)
    data = copy.deepcopy(profile.data or {})
    data[STATE_KEY] = {**state, "pending": rest}
    if cache is not None:
        data["ats_sniff_cache"] = {**(data.get("ats_sniff_cache") or {}), **cache}
    profile.data = data
    db.commit()
    report.update(looked_at=len(batch), new_boards=found_boards, remaining=len(rest))
    return report
