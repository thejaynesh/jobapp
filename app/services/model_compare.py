"""
Score the same stored jobs through several models and compare.

Switching the matching model is otherwise a leap of faith: the scores only
change on the next cycle, mixed in with new jobs, and by then it's hard to tell
a better model from a different sample. This runs candidates over identical
inputs so the comparison is like for like.

The most important column isn't the score — it's `unreadable`. Reasoning models
wrap their answer in thinking, and a reply the parser can't read means the job
doesn't get scored at all. A model that reasons beautifully but never emits
clean JSON is useless here, and this is what shows that up front.
"""

import copy
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.config import settings
from app.models.job import Job
from app.models.profile import Profile

logger = logging.getLogger(__name__)

# A comparison takes minutes, so the page that starts it is rarely the page that
# reads it: the whole lifecycle lives on the profile rather than in the request.
RESULT_KEY = "model_comparison"
ACTIVE_STATUSES = ("queued", "running")

# Past this, a record still claiming to be queued or running is a worker that
# died, not one that's slow — the lock it would have held expires sooner.
STALE_AFTER_SECONDS = 2700


@dataclass
class ModelResult:
    model: str
    scores: dict = field(default_factory=dict)      # job id -> score
    unreadable: int = 0                              # replies we couldn't parse
    errors: int = 0                                  # calls that failed outright
    seconds: float = 0.0

    @property
    def scored(self) -> int:
        return len(self.scores)

    @property
    def average(self) -> float:
        return round(sum(self.scores.values()) / len(self.scores), 1) if self.scores else 0.0


def sample_jobs(db: Session, limit: int) -> list[Job]:
    """
    Jobs with enough description to be worth scoring.

    Ordered by id so repeated runs use the same sample — comparing models
    against different jobs would defeat the point.
    """
    return (
        db.query(Job)
        .filter(Job.description.isnot(None))
        .filter(Job.description != "")
        .order_by(Job.id)
        .limit(limit)
        .all()
    )


def score_with_model(job, profile_data: dict, model: str) -> tuple[int | None, str]:
    """
    Score one job with one model.

    Returns (score, status) where status is "ok", "unreadable" or "error".
    Deliberately calls the primary provider only: the point is to judge this
    model, not to watch the fallback chain rescue it.
    """
    from app.services.matcher import (
        ResponseParseError,
        _build_match_prompt,
        _parse_llm_response,
        chat_completion,
    )

    messages = _build_match_prompt(job, profile_data)
    try:
        raw = chat_completion(
            messages=messages,
            api_key=settings.NVIDIA_NIM_API_KEY,
            base_url=settings.NVIDIA_NIM_BASE_URL,
            model=model,
        )
    except Exception as exc:
        logger.warning("compare: %s call failed for %s: %s", model, job.id, exc)
        return None, "error"

    try:
        return _parse_llm_response(raw)["score"], "ok"
    except ResponseParseError as exc:
        logger.warning("compare: %s gave an unreadable reply: %s", model, exc)
        return None, "unreadable"


def compare_models(
    db: Session, models: list[str], limit: int = 10, pace_seconds: float = 0.0,
) -> tuple[list[Job], list[ModelResult]]:
    """Run every model over the same job sample."""
    profile = db.query(Profile).first()
    profile_data = profile.data if profile else {}
    jobs = sample_jobs(db, limit)
    if not jobs:
        return [], []

    results = []
    for model in models:
        result = ModelResult(model=model)
        started = time.monotonic()
        for job in jobs:
            score, status = score_with_model(job, profile_data, model)
            if status == "ok":
                result.scores[str(job.id)] = score
            elif status == "unreadable":
                result.unreadable += 1
            else:
                result.errors += 1
            if pace_seconds:
                time.sleep(pace_seconds)
        result.seconds = round(time.monotonic() - started, 1)
        results.append(result)
        logger.info("compare: %s scored %d/%d jobs in %.1fs",
                    model, result.scored, len(jobs), result.seconds)

    return jobs, results


def report_dict(jobs: list[Job], results: list[ModelResult], threshold: int) -> dict:
    """
    The same comparison as `format_report`, shaped for the UI and for storage.

    Kept as plain data so it can sit on the profile and be rendered later —
    a comparison takes minutes, so the page that starts it is rarely the page
    that reads it.
    """
    rows = []
    for job in jobs:
        rows.append({
            "title": job.title,
            "company": job.company,
            "scores": {r.model: r.scores.get(str(job.id)) for r in results},
        })

    flips = []
    if len(results) >= 2:
        base, other = results[0], results[1]
        for job in jobs:
            a, b = base.scores.get(str(job.id)), other.scores.get(str(job.id))
            if a is None or b is None:
                continue
            if (a >= threshold) != (b >= threshold):
                flips.append({
                    "title": job.title, "company": job.company,
                    "from": a, "to": b,
                    "direction": "gained" if b >= threshold else "lost",
                })

    return {
        "threshold": threshold,
        "job_count": len(jobs),
        "models": [r.model for r in results],
        "rows": rows,
        "summary": [{
            "model": r.model, "scored": r.scored, "average": r.average,
            "unreadable": r.unreadable, "errors": r.errors, "seconds": r.seconds,
        } for r in results],
        "flips": flips,
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_state(db: Session) -> dict | None:
    """The stored comparison record, whatever stage it's at."""
    profile = db.query(Profile).first()
    return (profile.data.get(RESULT_KEY) if profile else None) or None


def store_state(db: Session, payload: dict) -> None:
    """Replace the comparison record. A missing profile means nowhere to put it."""
    profile = db.query(Profile).first()
    if profile is None:
        logger.warning("compare: no profile to store the result on")
        return
    data = copy.deepcopy(profile.data)
    data[RESULT_KEY] = payload
    profile.data = data
    db.commit()


def _blank(status: str, models: list[str], limit: int) -> dict:
    # The empty collections matter: the panel renders this record directly, and
    # a half-populated one would have it reaching for keys that aren't there.
    return {"status": status, "models": list(models), "job_count": limit,
            "rows": [], "summary": [], "flips": []}


def mark_queued(db: Session, models: list[str], limit: int) -> None:
    """
    Record the request before the worker sees it.

    Without this the panel has nothing to show between the click and the worker
    picking the task up — the lock isn't held yet, so "not running" and "never
    asked for" look identical.
    """
    store_state(db, {**_blank("queued", models, limit), "queued_at": _now()})


def mark_running(db: Session, models: list[str], limit: int) -> None:
    previous = load_state(db) or {}
    store_state(db, {**_blank("running", models, limit),
                     "queued_at": previous.get("queued_at"),
                     "started_at": _now()})


def _age_seconds(stamp: str | None) -> int | None:
    if not stamp:
        return None
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - moment).total_seconds()))


def _humanise(seconds: int | None) -> str:
    if seconds is None:
        return "a moment"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


def progress(record: dict | None) -> dict:
    """
    What the panel should say about a record, and whether to keep polling.

    `stalled` is the case worth naming: a worker that was killed mid-comparison
    leaves a record claiming to run forever, and polling that silently would be
    indistinguishable from a slow model.
    """
    if not record or record.get("status") not in ACTIVE_STATUSES:
        return {"active": False, "stalled": False, "stage": None, "waiting": ""}

    stage = record["status"]
    age = _age_seconds(record.get("started_at") or record.get("queued_at"))
    stalled = age is not None and age > STALE_AFTER_SECONDS
    return {"active": not stalled, "stalled": stalled, "stage": stage,
            "waiting": _humanise(age)}


def format_report(jobs: list[Job], results: list[ModelResult], threshold: int) -> str:
    """A side-by-side table, plus the disagreements that actually matter."""
    if not jobs or not results:
        return "No jobs with descriptions to compare — fetch some first."

    width = max(len(r.model) for r in results) + 2
    lines = ["", "Per-job scores", "-" * 60]
    header = f"{'JOB':<42}" + "".join(f"{r.model[-18:]:>{width}}" for r in results)
    lines.append(header)

    for job in jobs:
        label = f"{job.title[:26]} @ {job.company[:12]}"
        row = f"{label:<42}"
        for r in results:
            score = r.scores.get(str(job.id))
            row += f"{('—' if score is None else score):>{width}}"
        lines.append(row)

    lines += ["", "Summary", "-" * 60]
    for r in results:
        lines.append(
            f"  {r.model}\n"
            f"      scored {r.scored}/{len(jobs)}   avg {r.average}   "
            f"unreadable {r.unreadable}   errors {r.errors}   {r.seconds}s"
        )

    # Disagreements that cross the accept/reject line are the only ones that
    # change which jobs you actually see.
    if len(results) >= 2:
        base, other = results[0], results[1]
        flips = []
        for job in jobs:
            a, b = base.scores.get(str(job.id)), other.scores.get(str(job.id))
            if a is None or b is None:
                continue
            if (a >= threshold) != (b >= threshold):
                flips.append(f"      {job.title[:40]} @ {job.company[:18]}: "
                             f"{a} → {b}")
        lines += ["", f"Verdict flips at threshold {threshold} "
                      f"({base.model} → {other.model})", "-" * 60]
        lines += flips or ["      none — both models agree on every job"]

    if any(r.unreadable for r in results):
        lines += ["", "NOTE: an unreadable reply means the job isn't scored at "
                      "all. A model with any unreadable count is a poor fit for "
                      "this prompt, whatever its scores look like."]
    return "\n".join(lines)
