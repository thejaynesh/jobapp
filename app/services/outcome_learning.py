"""Mature cohorts and an experimental ranker; silence is never a negative label."""
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import joinedload

from app.models.application import Application
from app.models.intelligence import DecisionEvent
from app.services import application_history, for_you
from app.services.tunables import value

STORE_KEY = "outcome_model"


def cohort(db, profile, now=None):
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=int(value(profile, "outcome_maturity_days")))
    applications = (db.query(Application)
                    .filter(Application.applied_at.is_not(None))
                    .order_by(Application.applied_at.desc()).limit(2000).all())
    stages = application_history.milestones(db, [a.id for a in applications])
    channels = application_history.channels(db, [a.id for a in applications])
    groups = {}
    decisions = (db.query(DecisionEvent).filter(DecisionEvent.job_id.in_([a.job_id for a in applications]),
                 DecisionEvent.kind == "yes", DecisionEvent.payload["origin"].astext == "application")
                 .order_by(DecisionEvent.occurred_at).all()) if applications else []
    snapshots = {}
    for event in decisions:
        snapshots.setdefault(event.job_id, event)
    rows, immature, unknown = [], 0, 0
    for app in applications:
        channel = channels.get(app.id, "not recorded")
        group = groups.setdefault(channel, {"total": 0, "mature": 0, "unknown": 0, "interviews": 0})
        group["total"] += 1
        if app.applied_at > cutoff:
            immature += 1
            continue
        group["mature"] += 1
        milestones = stages.get(app.id, set())
        success = bool(milestones & {"interview_invited", "interview_completed", "offered"})
        group["interviews"] += int(success)
        failure = "rejected" in milestones
        snapshot = snapshots.get(app.job_id)
        if not success and not failure:
            unknown += 1
            group["unknown"] += 1
            continue
        rows.append({"application_id": str(app.id), "outcome": int(success), "at": app.applied_at,
                     "features": snapshot.payload.get("features") if snapshot else None,
                     "score": snapshot.payload.get("score") if snapshot else None,
                     "family": snapshot.payload.get("family") if snapshot else None})
    return {"rows": rows, "total": len(applications), "immature": immature, "unknown": unknown,
            "channels": groups,
            "mature": len(applications) - immature, "confirmed": len(rows), "limit": 2000,
            "window_days": int(value(profile, "outcome_maturity_days"))}


def report(db, profile, now=None):
    data = cohort(db, profile, now)
    return {**{k: v for k, v in data.items() if k != "rows"},
            "interviews": sum(row["outcome"] for row in data["rows"]),
            "model": profile.get(STORE_KEY) or {"reason": "No outcome experiment has run yet."}}


def fit(db, profile, now=None):
    now = now or datetime.now(timezone.utc)
    if value(profile, "outcome_mode") == "off":
        return {"mode": "off", "usable": False, "labels": 0, "reason": "Outcome learning disabled."}
    rows = sorted((r for r in cohort(db, profile, now)["rows"] if r["features"]), key=lambda r: r["at"])
    result = {"trained_at": now.isoformat(), "mode": "shadow", "usable": False, "labels": len(rows),
              "label": "Experimental outcome ordering; not a calibrated interview probability"}
    if len(rows) < value(profile, "outcome_min_labels"):
        return {**result, "reason": "Needs more mature, confirmed outcomes with decision-time snapshots."}
    size = max(10, len(rows) // 5)
    test = rows[-size:]
    families = {r["family"] for r in test}
    train = [r for r in rows[:-size] if r["family"] not in families]
    if len(train) < 10 or len({r["outcome"] for r in train}) < 2 or len({r["outcome"] for r in test}) < 2:
        return {**result, "reason": "Needs both outcomes in independent chronological train and holdout cohorts."}
    weights, bias = for_you.train([r["features"] for r in train], [r["outcome"] for r in train])
    weights, bias = for_you._prune(weights), round(bias, 5)
    labels = [r["outcome"] for r in test]
    learned = for_you.auc([for_you._predict(weights, bias, r["features"]) for r in test], labels)
    baseline = for_you.auc([r["score"] or 0 for r in test], labels)
    eligible = learned > max(.5, baseline) and min(sum(labels), len(labels) - sum(labels)) >= 5
    return {**result, "weights": weights, "bias": bias, "auc": learned, "score_auc": baseline,
            "holdout": len(test), "training": len(train), "beats_baseline": eligible, "version": 1,
            "validation": "chronological, grouped by posting family",
            "reason": "Eligible for opt-in assist; this small holdout remains uncertain." if eligible else "Shadow only: needs better ordering and at least five of each outcome in the holdout."}


def scores(profile, jobs, now=None):
    model = profile.get(STORE_KEY) or {}
    if value(profile, "outcome_mode") != "assist" or model.get("version") != 1 or not model.get("beats_baseline") or model.get("labels", 0) < value(profile, "outcome_min_labels"):
        return {}
    from app.services.experience import total_years
    years = total_years(profile.get("experience") or [])
    return {str(job.id): for_you._predict(model["weights"], model["bias"], for_you.features(job, years, now)) for job in jobs}
