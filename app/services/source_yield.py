"""
Whether each source earns its requests, and how much of what SimplifyJobs
lists our own readers reach.

The runs page already counts what each source *inserted* — but a source gets
that credit for any posting it happened to read first, even one three other
sources would have brought in a minute later. What says a source is worth
its cost is what only it found, and what only it found that the matcher
liked. `jobs.seen_by` records every source that has listed a job, so that is
now one aggregate.

The second number is recall. SimplifyJobs is a hand-curated list of US
early-career postings, and it is one of our sources, so its rows in the
database are a sample of the postings that exist: the share of them that
another source of ours also listed is the share our own readers reach. It was
measured by hand once (69% → 90% of hosts recognised, 2026-09-28); this
measures it daily, as postings actually found rather than hosts recognised,
and keeps the trend, so a reader that breaks shows as a falling line and the
ATS behind the misses is named.

Both from the database alone: no request goes out.
"""

import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from sqlalchemy import or_, text
from sqlalchemy.orm import Session

from app.models.job import Job

logger = logging.getLogger(__name__)

STATE_KEY = "source_yield"
REFERENCE = "simplify"
_HISTORY = 90
_MISSED_SHOWN = 10


def per_source(db: Session, days: int) -> list[dict]:
    """
    Each source's jobs first seen in the window: how many it listed, how many
    no other source did, and the same two for the ones matched.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = db.execute(text("""
        SELECT s AS source,
               count(*) AS seen,
               count(*) FILTER (WHERE cardinality(sources) = 1) AS only_here,
               count(*) FILTER (WHERE matched) AS matched,
               count(*) FILTER (WHERE matched AND cardinality(sources) = 1) AS matched_only_here
        FROM (
            SELECT coalesce(seen_by, ARRAY[source]::varchar[]) AS sources,
                   status IN ('matched', 'docs_generated') AS matched
            FROM jobs
            WHERE fetched_at >= :since
        ) AS recent
        CROSS JOIN LATERAL unnest(sources) AS s
        GROUP BY s
    """), {"since": since}).all()
    out = [{"source": r.source, "seen": r.seen, "only_here": r.only_here,
            "matched": r.matched, "matched_only_here": r.matched_only_here} for r in rows]
    out.sort(key=lambda r: (-r["matched_only_here"], -r["only_here"], r["source"]))
    return out


def _reader(url: str) -> str | None:
    """Which of our readers could reach this posting, if any."""
    from app.services import ats_discovery, posting_identity

    found = ats_discovery.extract_slugs(url or "")
    if found:
        return sorted(found)[0]
    canonical = posting_identity.canonical(url)
    if canonical:
        return urlparse(canonical).hostname
    return None


def recall(db: Session, days: int, roles: list[str]) -> dict:
    """
    Of the open postings SimplifyJobs listed in the window, how many another
    source of ours listed too — all of them, and those whose titles match the
    roles — and, for the ones missed, which system they are on.
    """
    from app.services.matcher import title_priority_match

    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (
        db.query(Job.title, Job.url, Job.source, Job.seen_by)
        .filter(Job.fetched_at >= since, Job.closed_at.is_(None),
                or_(Job.source == REFERENCE, Job.seen_by.contains([REFERENCE])))
        .all()
    )
    total = found = recognised = roles_total = roles_found = 0
    missed: Counter = Counter()
    for title, url, source, seen_by in rows:
        sources = set(seen_by or [source])
        if REFERENCE not in sources:
            continue
        also = bool(sources - {REFERENCE})
        reader = _reader(url)
        total += 1
        found += also
        recognised += reader is not None
        if roles and title_priority_match(title or "", roles):
            roles_total += 1
            roles_found += also
            if not also:
                missed[reader or (urlparse(url or "").hostname or "?").removeprefix("www.")] += 1
    return {
        "total": total, "found": found, "recognised": recognised,
        "roles_total": roles_total, "roles_found": roles_found,
        "missed": missed.most_common(_MISSED_SHOWN),
    }


def _share(part: int, whole: int) -> float | None:
    return round(100.0 * part / whole, 1) if whole else None


def measure(db: Session, profile_data: dict | None) -> dict:
    """Today's numbers, and the state to keep: the latest in full, the trend."""
    from app.services.tunables import value

    data = profile_data or {}
    days = int(value(data, "recall_window_days"))
    found = recall(db, days, list(data.get("target_roles") or []))
    today = datetime.now(timezone.utc).date().isoformat()
    latest = {
        "date": today, "window_days": days, **found,
        "share": _share(found["found"], found["total"]),
        "roles_share": _share(found["roles_found"], found["roles_total"]),
        "per_source": per_source(db, days),
    }
    state = dict(data.get(STATE_KEY) or {})
    history = [h for h in (state.get("history") or []) if h.get("date") != today]
    history.append({"date": today, "total": found["total"], "found": found["found"],
                    "roles_total": found["roles_total"], "roles_found": found["roles_found"],
                    "roles_share": latest["roles_share"], "share": latest["share"]})
    return {"latest": latest, "history": history[-_HISTORY:]}
