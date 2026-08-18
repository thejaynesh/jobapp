"""
Does the matcher still agree with you?

Every prompt edit and model swap in this repo has been made on the strength of
reading a few outputs and deciding they looked better. That is not nothing, but
it cannot catch a regression: a change that improves ten jobs and quietly
breaks forty looks exactly like a change that improves ten jobs.

So: a fixed set of jobs with a known verdict, scored through the current prompt
and the provider that actually scores your jobs, reported as agreement. Run it
before a change and after, and the difference is the evidence.

**Which provider.** Whatever `MATCH_PRIMARY` selects, because a number measured
against a provider the pipeline is not using is not evidence about the
pipeline — it is a fact about a road not taken, and it would read as the real
thing. Passing `--model` overrides that with a named NIM model, which is the
older "compare two NIM models" question.

**Where the labels come from.** Not from an afternoon of hand-labelling — from
decisions the user has already made. Applying to a job says it was a good fit;
marking one "not interested" says it was not; advancing an application to
interviewing says so emphatically. `build_labels` reads those out of the
database and writes them to a JSON file, which is then a plain text file the
user can edit, add to, or hand-write from scratch.

**Why the file carries a profile snapshot.** A score is a judgement about a
job *and* a candidate. Without freezing the profile, a run six weeks later
measures the profile drifting as much as the prompt changing — and the whole
point is to attribute a change to the thing that changed.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from app.models.application import Application, ApplicationStatus
from app.models.job import Job, JobStatus
from app.models.profile import Profile

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path("fixtures/match_labels.json")
FORMAT_VERSION = 1

GOOD, BAD = "good", "bad"

# Application statuses that say the user wanted this job. `not_applied` is
# absent on purpose: documents were generated for it and nothing happened,
# which is silence rather than approval.
_GOOD_APP_STATUSES = (
    ApplicationStatus.applied,
    ApplicationStatus.interviewing,
    ApplicationStatus.offered,
    # A rejection is the employer's verdict on the candidate, not the
    # candidate's on the job — they still wanted it enough to apply.
    ApplicationStatus.rejected,
)

# Filter reasons the user chose by hand. Everything else in that column is the
# pipeline's own opinion, which is exactly what this harness is auditing — so
# using it as ground truth would grade the matcher against itself.
_USER_REJECTIONS = ("manual", "blocked_title", "excluded_company")

# Enough description to be a real judgement rather than a coin toss.
_MIN_DESCRIPTION = 400

# Fields carried into the fixture so a label can be scored without the DB.
_JOB_FIELDS = (
    "title", "company", "location", "is_remote", "experience_level",
    "description", "required_years", "salary_min", "salary_max",
    "salary_currency", "employment_type", "required_skills",
    "nice_to_have_skills", "education_required",
)


@dataclass
class LabelledJob:
    """One job with a known verdict, self-contained enough to score."""
    verdict: str
    note: str = ""
    job_id: str = ""
    fields: dict = field(default_factory=dict)

    def as_job(self):
        """A job-shaped object the real prompt builder accepts."""
        return _FixtureJob(self.fields)


class _FixtureJob:
    """
    A stand-in for a `Job` row, carrying only what the prompt reads.

    Deliberately not a real `Job`: the fixture must score identically on a
    machine whose database has never seen these postings, which is what makes
    the file worth committing.
    """

    def __init__(self, fields: dict):
        for name in _JOB_FIELDS:
            setattr(self, name, fields.get(name))
        # No id: this is not a stored row, and the LLM log's job_id is a
        # foreign key. Handing it anything else made every insert in the batch
        # fail on a cast.
        self.id = None

    @property
    def salary_label(self):
        from app.models.job import Job as _Job

        return _Job.salary_label.fget(self)


# ---------------------------------------------------------------------------
# Building the fixture from what the user already decided
# ---------------------------------------------------------------------------

def _fields_for(job: Job) -> dict:
    return {name: getattr(job, name, None) for name in _JOB_FIELDS}


def build_labels(db: Session, limit_per_side: int = 25) -> list[LabelledJob]:
    """
    Read the user's own verdicts out of the database.

    Balanced between the two classes as far as the data allows: a set that is
    90% rejections scores 90% by agreeing with everything, and would report a
    matcher that says no to every job as excellent.
    """
    good = (
        db.query(Job)
        .join(Application, Application.job_id == Job.id)
        .filter(
            Application.status.in_(_GOOD_APP_STATUSES),
            Job.description.isnot(None),
        )
        .order_by(Job.id)
        .all()
    )
    bad = (
        db.query(Job)
        .filter(
            Job.status == JobStatus.filtered_out,
            Job.filter_reason.in_(_USER_REJECTIONS),
            Job.description.isnot(None),
        )
        .order_by(Job.id)
        .all()
    )

    labels: list[LabelledJob] = []
    for job in good[:limit_per_side]:
        if len(job.description or "") < _MIN_DESCRIPTION:
            continue
        labels.append(LabelledJob(
            verdict=GOOD, job_id=str(job.id), fields=_fields_for(job),
            note="you applied to this",
        ))
    for job in bad[:limit_per_side]:
        if len(job.description or "") < _MIN_DESCRIPTION:
            continue
        labels.append(LabelledJob(
            verdict=BAD, job_id=str(job.id), fields=_fields_for(job),
            note=f"you rejected this ({job.filter_reason})",
        ))
    return labels


def save(labels: list[LabelledJob], profile_data: dict,
         path: Path = DEFAULT_PATH) -> Path:
    """Write the fixture, profile snapshot and all."""
    payload = {
        "version": FORMAT_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        # Frozen on purpose: a score judges a job *and* a candidate, and
        # without this a run six weeks later measures the profile drifting as
        # much as the prompt changing.
        "profile": profile_data,
        "labels": [
            {"verdict": lab.verdict, "note": lab.note, "job_id": lab.job_id,
             **lab.fields}
            for lab in labels
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def load(path: Path = DEFAULT_PATH) -> tuple[list[LabelledJob], dict]:
    """Read a fixture back. Raises with a readable message if it isn't one."""
    if not path.exists():
        raise FileNotFoundError(
            f"No label file at {path}. Build one from your own decisions with "
            f"`python -m app.tasks.match_eval --build`."
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw = payload.get("labels")
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{path} has no labels in it.")

    labels = []
    for entry in raw:
        verdict = str(entry.get("verdict", "")).lower()
        if verdict not in (GOOD, BAD):
            raise ValueError(
                f"{path}: every label needs a verdict of {GOOD!r} or {BAD!r}; "
                f"found {entry.get('verdict')!r}."
            )
        labels.append(LabelledJob(
            verdict=verdict,
            note=entry.get("note", ""),
            job_id=entry.get("job_id", ""),
            fields={name: entry.get(name) for name in _JOB_FIELDS},
        ))
    return labels, payload.get("profile") or {}


# ---------------------------------------------------------------------------
# Running it
# ---------------------------------------------------------------------------

@dataclass
class EvalRow:
    verdict: str
    predicted: str
    score: int | None
    status: str            # ok | unreadable | error
    title: str
    company: str
    note: str = ""

    @property
    def agreed(self) -> bool:
        return self.status == "ok" and self.verdict == self.predicted


def _score_via_provider(provider, job, profile_data: dict) -> tuple[int | None, str]:
    """
    Score one job through a configured provider, in `score_with_model`'s shape.

    Routed through `call_provider` rather than the NIM helper for the same
    reason matching is: that is the path carrying the concurrency gate, and a
    harness that hammered a one-at-a-time endpoint would be measuring its own
    timeouts.
    """
    from app.llm.providers import call_provider
    from app.services import llm_log
    from app.services.matcher import (
        ResponseParseError,
        _build_match_prompt,
        _match_max_tokens,
        _parse_llm_response,
    )

    messages = _build_match_prompt(job, profile_data)
    try:
        with llm_log.stage("match_eval", job_id=getattr(job, "id", None)):
            raw = call_provider(
                provider, messages, temperature=0.1, max_tokens=_match_max_tokens(),
            )
    except Exception as exc:
        logger.warning("match_eval: %s call failed: %s", provider.name, exc)
        return None, "error"

    try:
        return _parse_llm_response(raw)["score"], "ok"
    except ResponseParseError as exc:
        logger.warning("match_eval: %s gave an unreadable reply: %s", provider.name, exc)
        return None, "unreadable"


def _scorer(profile_data: dict, model: str | None):
    """
    How to score, and what to call it in the report.

    With no model named this uses whatever actually scores jobs today — the
    provider `MATCH_PRIMARY` selects — because that is the entire point of the
    harness. An agreement figure measured against a provider the pipeline is
    not using is not evidence about the pipeline; it is a number about a road
    not taken, and it would read as the real thing.

    Naming a model keeps the older behaviour, which is a NIM model comparison:
    `--model meta/llama-3.3-70b-instruct` asks what that specific NIM model
    would do, whatever the primary happens to be.
    """
    from app.config import settings
    from app.llm.providers import primary_matching_provider, provider_label
    from app.services.model_compare import score_with_model

    if model is None:
        primary = primary_matching_provider()
        if primary is not None:
            return provider_label(primary), (
                lambda job: _score_via_provider(primary, job, profile_data)
            )

    named = model or settings.NVIDIA_NIM_MODEL
    return f"nim/{named}", (lambda job: score_with_model(job, profile_data, named))


def run(labels: list[LabelledJob], profile_data: dict,
        model: str | None = None, threshold: int | None = None) -> dict:
    """
    Score every labelled job through the current prompt and provider.

    Reports the two error directions separately, because they cost different
    things: a false reject is a job the user never sees, and a false accept is
    a document generation and a few minutes of their attention.
    """
    from app.services.tunables import value as tunable

    label_for_model, score_one = _scorer(profile_data, model)
    if threshold is None:
        threshold = tunable(profile_data, "min_match_score")

    rows: list[EvalRow] = []
    for label in labels:
        job = label.as_job()
        score, status = score_one(job)
        predicted = (
            GOOD if (status == "ok" and score is not None and score >= threshold)
            else BAD if status == "ok" else ""
        )
        rows.append(EvalRow(
            verdict=label.verdict, predicted=predicted, score=score,
            status=status, title=job.title or "", company=job.company or "",
            note=label.note,
        ))

    scored = [r for r in rows if r.status == "ok"]
    agreed = [r for r in scored if r.agreed]
    false_rejects = [r for r in scored if r.verdict == GOOD and r.predicted == BAD]
    false_accepts = [r for r in scored if r.verdict == BAD and r.predicted == GOOD]

    return {
        "model": label_for_model,
        "threshold": threshold,
        "labels": len(rows),
        "scored": len(scored),
        "unreadable": sum(1 for r in rows if r.status == "unreadable"),
        "errors": sum(1 for r in rows if r.status == "error"),
        "agreed": len(agreed),
        "agreement": round(100.0 * len(agreed) / len(scored), 1) if scored else 0.0,
        "false_rejects": len(false_rejects),
        "false_accepts": len(false_accepts),
        "disagreements": [
            {"verdict": r.verdict, "score": r.score, "title": r.title,
             "company": r.company, "note": r.note}
            for r in scored if not r.agreed
        ],
    }


def format_report(result: dict) -> str:
    """The result as something worth pasting into a commit message."""
    lines = [
        "",
        "Match-quality check",
        "-------------------",
        f"  model            {result['model']}",
        f"  accept threshold {result['threshold']}",
        f"  labels           {result['labels']}"
        f" ({result['scored']} scored, {result['unreadable']} unreadable,"
        f" {result['errors']} errored)",
        "",
        f"  AGREEMENT        {result['agreement']}%"
        f"  ({result['agreed']}/{result['scored']})",
        f"  false rejects    {result['false_rejects']}"
        "   (jobs you wanted, scored below the threshold)",
        f"  false accepts    {result['false_accepts']}"
        "   (jobs you rejected, scored above it)",
    ]
    if result["disagreements"]:
        lines += ["", "  Where it disagreed with you:"]
        for row in result["disagreements"][:20]:
            lines.append(
                f"    {row['score']:>3}  you said {row['verdict']:<4} "
                f"{row['title'][:44]:<44} {row['company'][:20]}"
            )
    if result["unreadable"]:
        lines += [
            "",
            f"  {result['unreadable']} replies could not be parsed. That is a "
            "model problem, not a scoring one — a model that never emits clean "
            "JSON scores nothing at all.",
        ]
    return "\n".join(lines) + "\n"


def profile_snapshot(db: Session) -> dict:
    profile = db.query(Profile).first()
    return dict(profile.data or {}) if profile else {}
