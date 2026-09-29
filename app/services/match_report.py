"""
How often the matcher agrees with what you then did, and what to change.

`match_eval` answers "is this prompt better than that one?" by re-scoring a
labelled set, which costs a model call per job and is run by hand. This
answers the everyday question for free, from scores already stored: of the
jobs you applied to or starred, how many did the matcher score above your
threshold; of the jobs you dismissed, how many did it put in front of you
anyway; and what threshold would have fitted your decisions better.

Decisions, read from what the user did rather than asked for:

* **yes**: an application past "not applied" (a rejection included: you
  wanted it enough to apply), or a starred job;
* **no**: a job dismissed from the jobs list, with its reason when one was
  given (`Job.dismiss_reason`). Title and company rules are left out: they
  filter future postings automatically, so a job carrying one was never
  judged by the user.

The threshold suggestion is deliberately conservative. It is the highest
threshold that still keeps nine in ten of your "yes" jobs, and it is made only
once there are enough decisions to mean something. Missing a job you would
have applied to costs more than reading one you would not.

The same sweep is run over the similarity pre-screen (`services/similarity`),
which is off until this report shows what a threshold on it would save and
cost.
"""

from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_

from app.models.application import Application, ApplicationStatus
from app.models.job import Job, JobStatus

# Application statuses that say the user wanted the job. As in match_eval:
# not_applied is silence, not approval.
YES_STATUSES = (ApplicationStatus.applied, ApplicationStatus.interviewing,
                ApplicationStatus.offered, ApplicationStatus.rejected)

KEEP_SHARE = 0.9
MIN_YES = 8
MIN_NO = 5
THRESHOLDS = tuple(range(30, 96, 5))
SIMILARITY_THRESHOLDS = tuple(range(5, 61, 5))
WEEK = timedelta(days=7)

# Why a job was dismissed, as the jobs list offers it, and where the setting
# that would have kept it out lives.
DISMISS_REASONS = {
    "too_senior": ("Too senior", "The junior threshold on the settings page, with the dates on "
                   "your experience, decides which senior titles are filtered before scoring."),
    "too_junior": ("Too junior", "Blocked title words on the profile (“intern”, "
                   "“junior”) filter these before any scoring."),
    "location": ("Wrong location", "Location preferences on the profile’s personal tab."),
    "stack": ("Wrong tech stack", "The skills the keyword filter counts (profile › skills) "
              "and the minimum skill matches setting."),
    "role": ("Not the kind of role", "Target roles and blocked title words on the profile."),
    "pay": ("Pay too low", "The minimum salary filter on the jobs list."),
    "company": ("Not this company", "“Not interested in this company” excludes its "
                "future postings too."),
    "sponsorship": ("No visa sponsorship", "The sponsorship filter on the jobs list."),
    "other": ("Something else", ""),
}

_SCORE = func.coalesce(Job.llm_score_deep, Job.llm_score)


def _score(job) -> float | None:
    return job.llm_score_deep if job.llm_score_deep is not None else job.llm_score


def decisions(db) -> list[dict]:
    """Every yes and no, newest first, with the scores the job had."""
    rows: dict = {}
    applied = (db.query(Job, Application)
               .join(Application, Application.job_id == Job.id)
               .filter(Application.status.in_(YES_STATUSES))
               .all())
    for job, application in applied:
        rows[job.id] = {
            "job": job, "verdict": "yes", "why": application.status.value,
            "at": application.applied_at or application.created_at,
        }
    for job in db.query(Job).filter(Job.favourite.is_(True)).all():
        rows.setdefault(job.id, {"job": job, "verdict": "yes", "why": "starred",
                                 "at": job.favourited_at})
    dismissed = (db.query(Job)
                 .filter(or_(Job.filter_reason == "manual", Job.dismiss_reason.isnot(None)))
                 .all())
    for job in dismissed:
        if job.id in rows:
            continue  # applied or starred, then dismissed: the yes stands
        rows[job.id] = {"job": job, "verdict": "no", "why": job.dismiss_reason or "",
                        "at": job.dismissed_at}
    found = []
    from app.models.intelligence import DecisionEvent
    snapshots = {}
    for event in db.query(DecisionEvent).filter(
            DecisionEvent.job_id.in_(list(rows)), DecisionEvent.kind.in_(["yes", "no", "reset"])).order_by(
                DecisionEvent.occurred_at.desc()):
        snapshots.setdefault(event.job_id, event)
    for row in rows.values():
        job = row["job"]
        snapshot = snapshots.get(job.id)
        payload = snapshot.payload if snapshot and snapshot.kind == row["verdict"] else {}
        found.append({**row, "score": payload.get("score", _score(job)), "similarity": job.similarity,
                      "features": payload.get("features"), "family": payload.get("family"),
                      "at": snapshot.occurred_at if payload else row["at"],
                      "title": job.title, "company": job.company, "id": job.id})
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(found, key=lambda r: r["at"] or epoch, reverse=True)


def agreement(rows: list[dict], threshold: float) -> dict:
    scored = [r for r in rows if r["score"] is not None]
    yes = [r for r in scored if r["verdict"] == "yes"]
    no = [r for r in scored if r["verdict"] == "no"]
    yes_kept = [r for r in yes if r["score"] >= threshold]
    no_shown = [r for r in no if r["score"] >= threshold]
    agreed = len(yes_kept) + (len(no) - len(no_shown))
    return {
        "yes": len(yes), "no": len(no), "unscored": len(rows) - len(scored),
        "yes_kept": len(yes_kept), "no_shown": len(no_shown),
        "agreed": agreed, "share": agreed / len(scored) if scored else None,
        "missed": sorted((r for r in yes if r["score"] < threshold), key=lambda r: r["score"]),
    }


def _at(t: float, rows: list[dict], weekly_scores: list[float]) -> dict:
    """What a minimum score of `t` would have kept, hidden and shown."""
    yes = [r["score"] for r in rows if r["score"] is not None and r["verdict"] == "yes"]
    no = [r["score"] for r in rows if r["score"] is not None and r["verdict"] == "no"]
    kept = sum(1 for s in yes if s >= t)
    return {
        "threshold": t,
        "yes_kept": kept,
        "yes_share": kept / len(yes) if yes else None,
        "no_hidden": sum(1 for s in no if s < t),
        "per_week": sum(1 for s in weekly_scores if s >= t),
    }


def sweep(rows: list[dict], weekly_scores: list[float]) -> list[dict]:
    return [_at(t, rows, weekly_scores) for t in THRESHOLDS]


def suggestion(rows: list[dict], table: list[dict], threshold: int,
               weekly_scores: list[float] = ()) -> dict:
    scored = [r for r in rows if r["score"] is not None]
    n_yes = sum(1 for r in scored if r["verdict"] == "yes")
    n_no = sum(1 for r in scored if r["verdict"] == "no")
    if n_yes < MIN_YES or n_no < MIN_NO:
        return {"kind": "wait", "need_yes": max(0, MIN_YES - n_yes),
                "need_no": max(0, MIN_NO - n_no)}
    keeping = [row for row in table if row["yes_share"] is not None
               and row["yes_share"] >= KEEP_SHARE]
    best = max((row["threshold"] for row in keeping), default=THRESHOLDS[0])
    current = _at(threshold, rows, list(weekly_scores))
    proposed = next(row for row in table if row["threshold"] == best)
    keeps_enough = current["yes_share"] is None or current["yes_share"] >= KEEP_SHARE
    # Within one step of the best, and keeping enough, is as good as there:
    # the table moves in fives, and "raise 72 to 75" is noise, not advice.
    step = THRESHOLDS[1] - THRESHOLDS[0]
    if keeps_enough and (best <= threshold or best - threshold < step):
        return {"kind": "keep", "threshold": threshold}
    return {"kind": "raise" if best > threshold else "lower", "threshold": best,
            "from": threshold, "row": proposed, "current": current}


def similarity_sweep(db, rows: list[dict], since: datetime) -> list[dict] | None:
    """What a pre-screen at each similarity would have skipped and lost."""
    recent = [s for (s,) in db.query(Job.similarity)
              .filter(Job.similarity.isnot(None), Job.fetched_at >= since, _SCORE.isnot(None))
              .all()]
    yes = [r["similarity"] for r in rows if r["verdict"] == "yes" and r["similarity"] is not None]
    if not recent:
        return None
    return [{
        "threshold": t,
        "calls_saved": sum(1 for s in recent if s < t),
        "scored": len(recent),
        "yes_lost": sum(1 for s in yes if s < t),
        "yes_known": len(yes),
    } for t in SIMILARITY_THRESHOLDS]


def reasons(rows: list[dict]) -> list[dict]:
    counts = Counter(r["why"] or "none" for r in rows if r["verdict"] == "no")
    return [{"key": key, "count": n,
             "label": DISMISS_REASONS.get(key, ("No reason given", ""))[0],
             "hint": DISMISS_REASONS.get(key, ("", ""))[1]}
            for key, n in counts.most_common()]


def build(db, profile_data: dict | None = None, now: datetime | None = None) -> dict:
    from app.services import tunables

    now = now or datetime.now(timezone.utc)
    since = now - WEEK
    threshold = int(tunables.value(profile_data, "min_match_score"))
    rows = decisions(db)
    week = [r for r in rows if r["at"] and r["at"] >= since]
    weekly_scores = [s for (s,) in db.query(_SCORE)
                     .filter(Job.fetched_at >= since, _SCORE.isnot(None),
                             Job.status.in_((JobStatus.matched, JobStatus.filtered_out,
                                             JobStatus.docs_generated)))
                     .all()]
    table = sweep(rows, weekly_scores)
    return {
        "threshold": threshold,
        "all": agreement(rows, threshold),
        "week": agreement(week, threshold),
        "table": table,
        "suggestion": suggestion(rows, table, threshold, weekly_scores),
        "similarity": similarity_sweep(db, rows, since),
        "prescreen": int(tunables.value(profile_data, "prescreen_min_similarity")),
        "reasons": reasons(rows),
        "recent": rows[:15],
        "scored_this_week": len(weekly_scores),
    }


def summary_line(report: dict) -> str:
    """One sentence for the weekly activity log entry."""
    week, total = report["week"], report["all"]
    parts = []
    if week["yes"] or week["no"]:
        parts.append(f"This week the matcher agreed with {week['agreed']} of your "
                     f"{week['yes'] + week['no']} scored decisions")
    else:
        parts.append("No scored decisions this week")
    if total["share"] is not None:
        parts.append(f"{round(total['share'] * 100)}% overall")
    advice = report["suggestion"]
    if advice["kind"] in ("raise", "lower"):
        parts.append(f"a minimum score of {advice['threshold']} would fit your decisions "
                     f"better than {advice['from']}")
    return "; ".join(parts) + "."
