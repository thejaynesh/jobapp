"""
Keep the verdict a job used to have.

Matching writes its answer into the job row, so every re-evaluation destroys
the one before it. That was invisible while jobs were scored once. Then
enrichment started sending jobs back to be scored again the moment their
description grew — which is the feature working — and the effect is that the
job most worth understanding, the one the pipeline changed its mind about,
is exactly the job whose earlier verdict no longer exists.

This appends a row before the overwrite. Three decisions in it are worth
stating, because each of them is the opposite of what `llm_log` does:

* **It writes on the caller's session.** `llm_log` deliberately uses its own,
  because a diagnostic must survive the rollback of the thing it is diagnosing.
  Here the opposite is correct: this row claims a job was scored 82 and filed
  as matched, and if that transaction rolls back the job was not. A history
  that records evaluations which never happened is worse than none.

* **It never raises, but it does not swallow much.** A failure to build the row
  is caught and logged; the row is then simply not added, and matching
  continues. What it must not do is leave a broken object in the caller's
  session, so everything that could fail happens before the `add`.

* **It prunes per job, not per table.** The LLM log keeps the last couple of
  thousand calls overall, which on a pipeline making thousands of calls a week
  means a job's first evaluation is gone within days. The first evaluation is
  the one you want. So the cap is per job, and twenty of them covers a job
  re-scored on every enrichment pass it will ever get.
"""

import logging
import uuid

from app.config import settings

logger = logging.getLogger(__name__)

# Per job, not per table — see the module docstring.
DEFAULT_KEEP_PER_JOB = 20


def _keep() -> int:
    try:
        return max(1, int(getattr(settings, "SCORE_HISTORY_KEEP_PER_JOB",
                                  DEFAULT_KEEP_PER_JOB)))
    except (TypeError, ValueError):
        return DEFAULT_KEEP_PER_JOB


def history(db, job_id, limit: int | None = None) -> list:
    """This job's evaluations, newest first."""
    from app.models.job_score import JobScore

    query = (
        db.query(JobScore)
        .filter(JobScore.job_id == job_id)
        .order_by(JobScore.created_at.desc(), JobScore.id.desc())
    )
    if limit is not None:
        query = query.limit(limit)
    return query.all()


def _trigger(db, job, previous) -> str:
    """
    Why this evaluation happened, worked out rather than plumbed through.

    Nothing that re-queues a job tells the matcher why — enrichment just sets
    the status back to `new` and walks away — and threading a reason through
    four callers to reach one column would be a lot of machinery for a fact
    that is already recoverable. A job with no history is being scored for the
    first time; a job whose description was replaced since its last verdict is
    being scored on new evidence, which is by far the common case; anything
    else is a plain re-score.
    """
    if previous is None:
        return "initial"
    grew_at = getattr(job, "description_updated_at", None)
    if grew_at is not None and previous.created_at is not None:
        try:
            if grew_at > previous.created_at:
                return "description_grew"
        except TypeError:
            # Naive/aware mismatch on a hand-built row. Not worth failing over.
            pass
    return "rescored"


def _prune(db, job_id, keep: int) -> None:
    """Make room for one more, oldest first."""
    from app.models.job_score import JobScore

    rows = (
        db.query(JobScore.id)
        .filter(JobScore.job_id == job_id)
        .order_by(JobScore.created_at.desc(), JobScore.id.desc())
        .offset(keep - 1)
        .all()
    )
    if not rows:
        return
    db.query(JobScore).filter(
        JobScore.id.in_([row[0] for row in rows])
    ).delete(synchronize_session=False)


def record(db, job, *, profile_data: dict | None = None,
           outcome: str | None = None):
    """
    Append this evaluation's verdict. Returns the row, or None if it wasn't
    recorded.

    Everything is read back off the job rather than passed in, because the job
    is where the evaluation just wrote its answer — and a second copy of that
    answer, assembled by the caller, is a second thing that can disagree with
    what the job actually says.
    """
    job_id = getattr(job, "id", None)
    if not isinstance(job_id, uuid.UUID) or db is None:
        # A job that was never stored — the match-quality fixture scores
        # exactly these — has no history to belong to.
        return None

    try:
        from app.models.job_score import JobScore

        min_score = None
        if profile_data is not None:
            from app.services.tunables import value as tunable

            min_score = tunable(profile_data, "min_match_score")

        llm_score = _float(getattr(job, "llm_score", None))
        deep = _float(getattr(job, "llm_score_deep", None))
        # The reasoning on the job belongs to whichever evaluation last ran a
        # model. A keyword rejection ran none, so carrying it over would
        # attribute an argument about skill overlap to a verdict that never
        # read the description.
        scored = llm_score is not None or deep is not None

        status = getattr(job, "status", None)
        status_text = getattr(status, "value", None) or (
            str(status) if status is not None else ""
        )

        previous = None
        rows = history(db, job_id, limit=1)
        if rows:
            previous = rows[0]

        _prune(db, job_id, _keep())

        row = JobScore(
            job_id=job_id,
            score=deep if deep is not None else llm_score,
            llm_score=llm_score,
            llm_score_deep=deep,
            matched_by=_text(getattr(job, "matched_by", None)),
            deep_matched_by=_text(getattr(job, "deep_matched_by", None)),
            status=(outcome or status_text or "")[:60],
            filter_reason=_text(getattr(job, "filter_reason", None)),
            filter_detail=_text(getattr(job, "filter_detail", None)),
            reasoning=_text(getattr(job, "llm_reasoning", None)) if scored else None,
            min_score=_float(min_score),
            description_chars=len(getattr(job, "description", None) or ""),
            trigger=_trigger(db, job, previous),
        )
    except Exception as exc:
        # Nothing has been added to the session at this point, so matching
        # carries on with a gap in the history rather than a failed commit.
        logger.warning("score_history: could not record an evaluation for %s: %s",
                       job_id, exc)
        return None

    db.add(row)
    return row


def _float(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _text(value):
    if not isinstance(value, str):
        return None
    return value or None
