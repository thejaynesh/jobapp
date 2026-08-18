"""
Where the jobs go: fetched, filtered, matched, written, applied.

Every number in this app has existed for a while, in the sense that it could be
derived from a query somebody was willing to write. What has not existed is the
shape they make together — and that shape is the only thing that answers the
question the whole pipeline is for. A hundred and fifty thousand jobs fetched
and forty applications sent is either a working filter or a broken one, and
which it is depends entirely on what happened in between.

Three views, because three different things go wrong.

**The funnel** says where the volume stops. One filter reason accounting for
most of the drop is either the system working exactly as intended or a rule
that is far too aggressive, and both look like a big number until you read what
it says.

**Source ROI** says which fetching is worth doing. A source returning thousands
of postings and inserting none is spending requests to re-read jobs already
stored, and that only shows up as a ratio.

**Score distribution per model** says whether a model is scoring or guessing.
Everything clustered at 70 is not a careful judgement, it is a model that has
found a safe answer — and comparing two models' distributions is the fastest
way to see it.

One thing this deliberately does not claim. The status columns hold the current
state, not a history, so "matched over time" is a cohort view — of the jobs
fetched on a given day, how many are matched *now*. That is a different
statement from a flow, and it is the one the data actually supports.
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import Float, Integer, case, func

from app.models.application import Application, ApplicationStatus
from app.models.job import Job, JobStatus

logger = logging.getLogger(__name__)

DEFAULT_DAYS = 30

# Score bands. Wide enough that a band is a statement ("mostly rejected") rather
# than noise, narrow enough to show a model piling everything into one answer.
_BANDS = ((0, 25), (25, 50), (50, 65), (65, 75), (75, 85), (85, 101))


def _since(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=max(1, days))


def overview(db) -> dict:
    """
    The whole funnel as counts, with the drop explained.

    `filtered` is broken out by reason because the total on its own is
    uninterpretable: a hundred thousand jobs dropped on a title mismatch is the
    system working, and the same number dropped for having no description is a
    fetching problem wearing a filter's clothes.
    """
    from app.services.matcher import FILTER_REASON_LABELS

    status_rows = dict(
        db.query(Job.status, func.count(Job.id)).group_by(Job.status).all()
    )
    counts = {status.value: int(status_rows.get(status, 0)) for status in JobStatus}
    total = sum(counts.values())

    live_reasons = (
        db.query(Job.filter_reason, func.count(Job.id))
        .filter(Job.status == JobStatus.filtered_out)
        .group_by(Job.filter_reason)
        .all()
    )
    # Archived rejections are counted too. This panel exists to say where the
    # volume goes, and the day archiving first runs it would otherwise appear
    # that a hundred thousand jobs were never filtered at all — a breakdown
    # that silently stops counting most of its subject is worse than one that
    # is merely incomplete.
    from app.services.archive import reasons as archived_reasons

    try:
        archived = archived_reasons(db)
    except Exception as exc:
        logger.warning("funnel: archived reasons unavailable: %s", exc)
        archived = {}
    archived_total = sum(archived.values())

    tally: dict[str | None, int] = {}
    for reason, count in live_reasons:
        tally[reason] = tally.get(reason, 0) + int(count)
    for reason, count in archived.items():
        key = None if reason == "unknown" else reason
        tally[key] = tally.get(key, 0) + int(count)
    reasons = sorted(tally.items(), key=lambda item: -item[1])
    filtered_total = counts["filtered_out"] + archived_total

    app_rows = dict(
        db.query(Application.status, func.count(Application.id))
        .group_by(Application.status)
        .all()
    )
    applications = {
        status.value: int(app_rows.get(status, 0)) for status in ApplicationStatus
    }
    sent = sum(
        applications[name] for name in
        ("applied", "interviewing", "offered", "rejected")
    )

    return {
        # Archived jobs are still jobs this pipeline fetched and judged. A
        # total that shrank every night as the archiver ran would describe the
        # size of one table rather than the work the system has done.
        "total": total + archived_total,
        "live": total,
        "archived": archived_total,
        "by_status": counts,
        "filter_reasons": [
            {
                "reason": reason or "unknown",
                "label": FILTER_REASON_LABELS.get(reason, "Not recorded"),
                "count": int(count),
                "share": round(100.0 * count / filtered_total, 1)
                if filtered_total else 0.0,
            }
            for reason, count in reasons
        ],
        "applications": applications,
        "sent": sent,
        # The one ratio that describes the whole machine — over every job ever
        # fetched, archived ones included, or it would drift upwards every
        # night as the archiver ran without anything actually improving.
        "sent_per_thousand": (
            round(1000.0 * sent / (total + archived_total), 2)
            if (total + archived_total) else 0.0
        ),
    }


def cohorts(db, days: int = DEFAULT_DAYS) -> list[dict]:
    """
    Of the jobs fetched on each day, what became of them.

    A cohort rather than a flow, because the job row holds its current status
    and not a history of it — see the module docstring. Read downwards: a day
    whose jobs are still mostly `new` has a matching backlog, and a day with a
    thousand fetched and two matched is a day the fetchers found the wrong
    jobs.
    """
    day = func.date_trunc("day", Job.fetched_at)
    rows = (
        db.query(
            day.label("day"),
            func.count(Job.id),
            func.sum(case((Job.status == JobStatus.new, 1), else_=0)),
            func.sum(case((Job.status == JobStatus.filtered_out, 1), else_=0)),
            func.sum(case((Job.status == JobStatus.matched, 1), else_=0)),
            func.sum(case((Job.status == JobStatus.docs_generated, 1), else_=0)),
        )
        .filter(Job.fetched_at >= _since(days))
        .group_by(day)
        .order_by(day.desc())
        .all()
    )
    return [
        {
            "day": when,
            "fetched": int(fetched or 0),
            "new": int(new or 0),
            "filtered": int(filtered or 0),
            "matched": int(matched or 0),
            "docs": int(docs or 0),
        }
        for when, fetched, new, filtered, matched, docs in rows
    ]


def source_roi(db, runs: int = 20) -> list[dict]:
    """
    What each source contributes per unit of work it does.

    The ratio is new jobs per thousand *fetched*, not per thousand requests.
    Nothing counts requests — a board source makes one per company and an API
    source makes one per page — so a per-request figure would be invented. Per
    fetched is the honest version of the same question: of everything this
    source handed over, how much of it had we not already stored.
    """
    from app.services.fetch_history import source_totals

    out = []
    for row in source_totals(db, runs):
        fetched = row["fetched"]
        out.append({
            **row,
            "new_per_thousand": round(1000.0 * row["inserted"] / fetched, 1)
            if fetched else 0.0,
            # A source that returns nothing at all is a different failure from
            # one that returns plenty and contributes none, and the ratio alone
            # cannot tell them apart — both are zero.
            "silent": fetched == 0,
        })
    out.sort(key=lambda item: (-item["new_per_thousand"], -item["inserted"]))
    return out


def score_distribution(db, days: int | None = None) -> list[dict]:
    """
    How each model spreads its scores.

    A model whose scores all land between 65 and 75 is not judging, it has
    found an answer that is never badly wrong — and that is invisible in an
    average, which is exactly what an average of that distribution looks like
    for a model that is genuinely discriminating.

    Both passes are reported separately (see the second-opinion pass in
    `matcher._deep_score`), because the whole reason for keeping both numbers
    is to compare them.
    """
    rows = []
    for label, model_column, score_column in (
        ("first pass", Job.matched_by, Job.llm_score),
        ("second look", Job.deep_matched_by, Job.llm_score_deep),
    ):
        query = db.query(
            model_column,
            func.count(Job.id),
            func.avg(func.cast(score_column, Float)),
            *[
                func.sum(case(
                    ((score_column >= low) & (score_column < high), 1), else_=0,
                ))
                for low, high in _BANDS
            ],
        ).filter(model_column.isnot(None), score_column.isnot(None))
        if days:
            query = query.filter(Job.fetched_at >= _since(days))

        for record in query.group_by(model_column).all():
            model, count, average = record[0], int(record[1] or 0), record[2]
            bands = [int(value or 0) for value in record[3:]]
            rows.append({
                "pass": label,
                "model": model,
                "count": count,
                "average": round(float(average), 1) if average is not None else None,
                "bands": [
                    {"low": low, "high": high, "count": value,
                     "share": round(100.0 * value / count, 1) if count else 0.0}
                    for (low, high), value in zip(_BANDS, bands)
                ],
            })
    rows.sort(key=lambda item: (item["pass"], -item["count"]))
    return rows


def second_opinion(db) -> dict:
    """
    Whether the second pass earns what it costs.

    Two numbers decide it: how far it moves a score on average, and how often
    that movement crosses the accept threshold. A pass that shifts scores by
    three points and never changes an outcome is a paid call buying agreement.
    """
    rows = (
        db.query(
            func.count(Job.id),
            func.avg(func.cast(Job.llm_score_deep - Job.llm_score, Float)),
            func.sum(case(
                ((Job.status == JobStatus.filtered_out)
                 & (Job.llm_score_deep < Job.llm_score), 1), else_=0,
            )),
            func.sum(case(
                ((Job.status != JobStatus.filtered_out)
                 & (Job.llm_score_deep > Job.llm_score), 1), else_=0,
            )),
        )
        .filter(Job.llm_score_deep.isnot(None), Job.llm_score.isnot(None))
        .first()
    )
    count, shift, dropped, rescued = rows or (0, None, 0, 0)
    return {
        "rescored": int(count or 0),
        "avg_shift": round(float(shift), 1) if shift is not None else None,
        "dropped": int(dropped or 0),
        "rescued": int(rescued or 0),
    }


def enrichment_effect(db) -> dict:
    """
    What going back for the real description changed.

    Reads the score history rather than the job, because the job only holds the
    verdict that stuck. A job re-scored on a fuller description is the one case
    where the pipeline has demonstrably changed its own mind, and counting
    those is the closest thing to a direct measurement of whether enrichment is
    worth its calls.
    """
    from app.models.job_score import JobScore

    rows = (
        db.query(
            func.count(JobScore.id),
            func.sum(case((JobScore.status == "matched", 1), else_=0)),
            func.avg(func.cast(JobScore.description_chars, Integer)),
        )
        .filter(JobScore.trigger == "description_grew")
        .first()
    )
    count, matched, chars = rows or (0, 0, None)
    return {
        "rescored": int(count or 0),
        "now_matched": int(matched or 0),
        "avg_chars": int(chars) if chars is not None else 0,
    }
