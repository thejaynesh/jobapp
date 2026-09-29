"""Frozen human-labelled comparisons. No inference without an explicit paid flag."""
import copy
import time

from app.services import evidence, match_eval


def compare(labels, profile, *, allow_paid=False, rounds=1, max_calls=100, model=None):
    if not allow_paid:
        raise ValueError("Model evaluation requires --allow-paid; deterministic regression tests make no paid calls.")
    if not 1 <= rounds <= 3 or 2 * rounds * len(labels) > max_calls or not labels:
        raise ValueError("Use 1–3 rounds and a non-empty fixture within the explicit call budget.")
    digest = evidence.fingerprint({"profile": profile, "labels": [vars(label) for label in labels]})
    runs = []
    for iteration in range(rounds):
        for mode in ("shadow", "assist"):
            candidate = copy.deepcopy(profile)
            candidate.setdefault("settings", {})["match_evidence_mode"] = mode
            before = time.monotonic()
            report = match_eval.run(labels, candidate, model=model)
            report.update({"round": iteration + 1, "mode": mode, "elapsed_seconds": round(time.monotonic() - before, 3)})
            runs.append(report)
    return {"version": 1, "fixture_hash": digest, "evidence_version": evidence.VERSION,
            "labels": len(labels), "requested_assessments": len(labels) * 2 * rounds,
            "runs": runs, "promotion": "Human review required: compare recall, top-ten usefulness, factuality, latency and provider costs. No automatic setting changes.",
            "cost_note": "Provider attempts, token usage and estimated costs are recorded by the existing LLM log; retries can exceed requested assessments."}


def rubrics():
    return {
        "recommendation": ["worth applying", "false rejection", "priority among this week's opportunities", "unsupported requirement claims"],
        "document": ["claims supported by original employer/project", "important evidence survived the PDF", "preferred over baseline", "minutes spent correcting"],
        "browser": ["correct verified values", "user values preserved", "declarations left unanswered when unknown", "submission supported by visible confirmation"],
        "split": "Freeze the fixture before tuning. Hold out later decisions and whole posting families. Include manual audits of rejected jobs.",
    }
