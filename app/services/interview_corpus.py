"""
Storing interview writeups, and — the part that matters — choosing which to read.

Ingestion is the easy half. Forty reports about one company is not forty times
more useful than one; most of them describe a loop that no longer exists. So
everything here is built around ranking, and the ranking is deliberately
explainable rather than clever: a score you can read off the fields beats one
that is better on average but cannot be argued with when it is wrong.

Three factors, in the order they matter.

**Recency, weighted hard.** Interview loops change. A report from last quarter
describes the process you are about to enter; one from 2019 describes a company
that has since reorganized twice. Weight decays over about eighteen months and
reports past three years are dropped unless nothing else exists — the exception
matters, because "old information about this company" still beats "no
information".

**Level match.** "Amazon SDE-1 University Grad" and "Amazon SDE-2 lateral" are
different loops with different rounds. A report whose role hint matches what you
are applying for is worth several that do not.

**Company match**, through the same `company_key` normalization the rest of the
app uses, so the corpus lines up with jobs and contacts without a second notion
of who a company is.
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func

from app.models.interview_report import REPORT_SOURCES, InterviewReport
from app.services.company_domain import company_key as normalize_company

logger = logging.getLogger(__name__)

# Reports stop being representative long before they stop being readable.
FRESH_DAYS = 180          # full weight
DECAY_DAYS = 550          # ~18 months: weight falls off across this span
STALE_DAYS = 365 * 3      # past this, only used when there is nothing else

# Level vocabulary, matched as substrings against a report's role hint. Loose on
# purpose: every company names its levels differently, and a strict taxonomy
# would mostly produce misses.
_LEVEL_ALIASES = {
    "intern": ("intern", "internship", "summer"),
    "new_grad": ("new grad", "newgrad", "university", "campus", "fresher", "entry", "sde-1", "sde 1", "l3", "e3"),
    "mid": ("sde-2", "sde 2", "l4", "e4", "mid", "software engineer ii", "swe ii"),
    "senior": ("senior", "sde-3", "sde 3", "l5", "e5", "staff", "principal", "lead"),
}


class CorpusError(Exception):
    """An ingest that could not proceed, phrased for the caller."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def level_of(text: str) -> str | None:
    """The level a role hint describes, or None when it does not say."""
    lowered = (text or "").lower()
    if not lowered.strip():
        return None
    for level, aliases in _LEVEL_ALIASES.items():
        if any(alias in lowered for alias in aliases):
            return level
    return None


def recency_weight(posted_at: datetime, now: datetime | None = None) -> float:
    """
    How much a report of this age is worth, from 1.0 down to 0.0.

    Flat for the first six months rather than decaying immediately: a report
    from March and one from June describe the same loop, and pretending
    otherwise would rank on noise.
    """
    now = now or _now()
    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=timezone.utc)
    age_days = (now - posted_at).days

    if age_days < 0:
        # A future date is a parsing error somewhere upstream. Treat it as
        # current rather than as infinitely valuable.
        return 1.0
    if age_days <= FRESH_DAYS:
        return 1.0
    if age_days >= STALE_DAYS:
        return 0.0
    decayed = 1.0 - (age_days - FRESH_DAYS) / DECAY_DAYS
    return max(0.0, min(1.0, decayed))


def score(report: InterviewReport, level: str | None = None, now: datetime | None = None) -> float:
    """
    How much this report is worth reading, for someone applying at `level`.

    Recency dominates, level adjusts. Length contributes a little because a
    two-line "I got rejected" carries almost nothing next to a full writeup —
    but only a little, since a rambling post is not therefore informative.
    """
    weight = recency_weight(report.posted_at, now)
    if weight <= 0:
        return 0.0

    if level:
        report_level = level_of(report.role_hint or "") or level_of(report.title or "")
        if report_level == level:
            weight *= 1.5
        elif report_level is not None:
            # It names a different level, so it describes a different loop.
            weight *= 0.6

    body_length = len(report.body or "")
    substance = min(1.0, body_length / 2000.0)
    return round(weight * (0.75 + 0.25 * substance), 4)


def ingest(db, reports: list[dict]) -> dict:
    """
    Store fetched reports, skipping the ones that cannot be ranked.

    Returns counts including `undated`, which is reported rather than silently
    folded into "skipped": a source that suddenly stops supplying dates has
    broken in a way worth seeing, and it looks identical to a quiet source
    otherwise.
    """
    counts = {"stored": 0, "duplicate": 0, "undated": 0, "invalid": 0}

    for data in reports:
        url = (data.get("url") or "").strip()
        company = (data.get("company") or "").strip()
        if not url or not company or data.get("source") not in REPORT_SOURCES:
            counts["invalid"] += 1
            continue

        posted_at = data.get("posted_at")
        if not isinstance(posted_at, datetime):
            counts["undated"] += 1
            continue
        if posted_at.tzinfo is None:
            posted_at = posted_at.replace(tzinfo=timezone.utc)

        if db.query(InterviewReport).filter(InterviewReport.url == url).first():
            counts["duplicate"] += 1
            continue

        db.add(
            InterviewReport(
                company_key=normalize_company(company),
                company=company,
                source=data["source"],
                url=url,
                title=(data.get("title") or "")[:500],
                body=(data.get("body") or "")[:60000],
                posted_at=posted_at,
                role_hint=(data.get("role_hint") or None),
            )
        )
        counts["stored"] += 1

    db.commit()
    return counts


def reports_for(
    db,
    company: str,
    level: str | None = None,
    limit: int = 20,
    now: datetime | None = None,
) -> list[InterviewReport]:
    """
    The most useful reports about this company, best first.

    Stale reports are excluded — unless excluding them would leave nothing, in
    which case they come back. Old information about a company beats none, and
    silently returning an empty list when the corpus does hold something would
    be the wrong kind of strict.
    """
    key = normalize_company(company)
    if not key:
        return []

    rows = (
        db.query(InterviewReport)
        .filter(InterviewReport.company_key == key)
        .order_by(InterviewReport.posted_at.desc())
        .limit(500)
        .all()
    )
    if not rows:
        return []

    now = now or _now()
    ranked = sorted(rows, key=lambda r: score(r, level, now), reverse=True)
    fresh = [r for r in ranked if score(r, level, now) > 0]
    return (fresh or ranked)[:limit]


def coverage(db, company: str | None = None) -> dict:
    """
    What the corpus actually holds — per source, and how fresh.

    Exists because a corpus that quietly stopped growing looks exactly like one
    nobody has queried. Both the fetch task and the UI read this.
    """
    query = db.query(
        InterviewReport.source,
        func.count(InterviewReport.id),
        func.max(InterviewReport.posted_at),
    )
    if company:
        key = normalize_company(company)
        query = query.filter(InterviewReport.company_key == key)

    by_source = {
        source: {"count": count, "newest": newest.isoformat() if newest else None}
        for source, count, newest in query.group_by(InterviewReport.source).all()
    }
    for source in REPORT_SOURCES:
        by_source.setdefault(source, {"count": 0, "newest": None})

    total = sum(entry["count"] for entry in by_source.values())
    recent_cutoff = _now() - timedelta(days=FRESH_DAYS)
    recent_query = db.query(func.count(InterviewReport.id)).filter(
        InterviewReport.posted_at >= recent_cutoff
    )
    if company:
        recent_query = recent_query.filter(
            InterviewReport.company_key == normalize_company(company)
        )

    return {
        "total": total,
        "recent": recent_query.scalar() or 0,
        "by_source": by_source,
    }
