"""A small, explainable action plan built from bounded queries and existing facts."""
from collections import Counter
from datetime import datetime, timezone

from sqlalchemy import or_
from sqlalchemy.orm import joinedload, selectinload

from app.models.application import Application, ApplicationStatus
from app.models.job import Job, JobStatus
from app.services import evidence
from app.services.tunables import value


def build(db, profile, minutes=None, now=None):
    now = now or datetime.now(timezone.utc)
    budget = max(5, min(240, int(minutes if minutes is not None else value(profile, "today_minutes"))))
    actions = []
    due = db.query(Application).options(joinedload(Application.job)).filter(
        Application.next_action_due <= now.date(),
        Application.status.in_([ApplicationStatus.applied, ApplicationStatus.interviewing, ApplicationStatus.offered]),
    ).order_by(Application.next_action_due, Application.id).limit(30).all()
    for application in due:
        actions.append({"kind": "followup", "title": application.next_action or "Review next step",
            "company": application.job.company, "job": application.job, "url": f"/apps/{application.id}",
            "minutes": int(value(profile, "plan_followup_minutes")), "priority": 1000,
            "reason": f"Due {application.next_action_due.isoformat()}; recorded on your application.",
            "evidence": [], "question_id": ""})

    available = db.query(Job).options(selectinload(Job.applications)).filter(
        Job.closed_at.is_(None), ~Job.applications.any(Application.status != ApplicationStatus.not_applied))
    candidates = available.filter(Job.status.in_([JobStatus.matched, JobStatus.docs_generated])).order_by(
        Job.favourite.desc(), Job.llm_score.desc().nullslast(), Job.fetched_at.desc()).limit(60).all()
    exploration = []
    if int(value(profile, "today_exploration_percent")):
        exploration = available.filter(Job.status == JobStatus.filtered_out,
            Job.filter_reason.in_(["low_score", "low_similarity"]), Job.dismiss_reason.is_(None)).order_by(
            Job.fetched_at.desc()).limit(10).all()
    questions = Counter()
    answered = evidence.active_answers(profile)
    question_rows = {}
    pins = {evidence.normal(name) for name in profile.get("plan_pins") or []}
    from app.services import semantic, outcome_learning
    semantic_scores = semantic.cached_scores(db, candidates, profile)
    outcome_scores = outcome_learning.scores(profile, candidates, now)
    for job in candidates + exploration:
        assessment = evidence.current(job, profile)
        for row in assessment["requirements"]:
            if row["status"] == "unknown" and row["priority"] == "required" and row["id"] not in answered:
                questions[row["id"]] += 1
                question_rows[row["id"]] = row
        score = job.llm_score_deep if job.llm_score_deep is not None else job.llm_score
        is_exploration = job in exploration
        priority = (score or 40) + (10 if job.favourite else 0) + (8 if evidence.normal(job.company) in pins else 0)
        if value(profile, "semantic_mode") == "assist":
            priority += 8 * semantic_scores.get(str(job.id), 0)
        priority += min(assessment["supported"], 5)
        priority += 5 * outcome_scores.get(str(job.id), 0)
        ready = job.status == JobStatus.docs_generated
        reason = (f"{assessment['supported']} requirements have supporting evidence; "
                  f"{assessment['unknown']} need clarification. " if assessment["requirements"] else "Requirements have not yet been extracted. ")
        reason += "Documents are ready to review." if ready else "Prepare and review documents before applying."
        if job.favourite:
            reason += " You starred this job."
        if evidence.normal(job.company) in pins:
            reason += " You prioritized this employer."
        if str(job.id) in outcome_scores:
            reason += " Experimental outcome ordering contributes a small preference signal."
        if value(profile, "semantic_mode") == "assist" and str(job.id) in semantic_scores:
            reason += " Cached semantic relevance contributes to this ordering."
        actions.append({"kind": "explore" if is_exploration else "apply", "job": job,
            "title": job.title, "company": job.company, "url": f"/jobs/{job.id}/application",
            "minutes": max(1, round(budget * value(profile, "today_exploration_percent") / 100)) if is_exploration else int(value(profile, "plan_application_minutes")), "priority": priority,
            "reason": ("Brief review of an overlooked posting; decide whether to shortlist it. " if is_exploration else "") + reason,
            "evidence": assessment["requirements"], "question_id": ""})
    for key, count in questions.most_common(3):
        row = question_rows[key]
        actions.append({"kind": "clarify", "job": None, "title": row["question"], "company": "Your evidence",
            "url": "", "minutes": 3, "priority": 110 + count, "reason": f"An answer helps assess {count} shortlisted opportunities.",
            "evidence": [row], "question_id": key})
    actions.sort(key=lambda row: (-row["priority"], row["title"]))
    chosen, families, spent = [], set(), 0
    exploration_budget = max(1, round(budget / max(1, int(value(profile, "plan_application_minutes"))) * int(value(profile, "today_exploration_percent")) / 100))
    # Reserve an exploration slot only when it fits alongside useful work.
    explorers = [a for a in actions if a["kind"] == "explore"][:exploration_budget]
    regular = [a for a in actions if a["kind"] != "explore"]
    if explorers and budget >= explorers[0]["minutes"] + int(value(profile, "plan_application_minutes")):
        index = next((i for i, a in enumerate(regular) if a["kind"] != "followup"), len(regular))
        regular.insert(index, explorers[0])
    for action in regular:
        family = evidence.normal(action["company"]) if action["kind"] in {"apply", "explore"} else None
        if family and family in families or spent + action["minutes"] > budget:
            continue
        chosen.append(action)
        spent += action["minutes"]
        if family:
            families.add(family)
    return {"actions": chosen, "minutes": budget, "planned_minutes": spent,
            "due_count": len(due), "deferred_count": len(actions) - len(chosen),
            "semantic_scores": semantic_scores, "pins": profile.get("plan_pins") or []}
