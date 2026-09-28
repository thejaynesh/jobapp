"""
Answers to application questions the profile does not cover, kept once given.

Every form asks a few questions no profile field anticipates — "Are you open
to hybrid work?", "Do you have a non-compete?" — and they come round again on
the next form in the same words. The extension's "Remember my answers" sends
what the user typed into those; the next fill types it back for a question
worded the same way. job_app_filler and autograph (open-source autofill
extensions) keep the same kind of store in the browser; this keeps it on the
user's own server, where the profile page lists it and one click forgets it.

Only short answers: long-form text is usually about one company ("Why do you
want to work here?"), so it is drafted fresh for each form instead
(`answer_drafts`) and never kept here; and this refuses anything that looks
like a credential or identity document, whoever sends it.
"""

import re
from datetime import datetime, timezone

STORE_KEY = "remembered_answers"
MAX_ANSWERS = 500
MAX_QUESTION = 300
MAX_ANSWER = 500

_SENSITIVE = re.compile(
    r"(password|passcode|social security|\bssn\b|date of birth|birth ?date|\bdob\b|bank|"
    r"routing|account number|credit card|card number|\bcvv\b|licen[cs]e number|"
    r"passport number)", re.I)


def normalize_question(text: str) -> str:
    """The key a question is matched by — the same rule as autofill.js."""
    text = re.sub(r"\(required\)|\*", " ", text or "", flags=re.I)
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())[:MAX_QUESTION]


def entries(profile_data: dict) -> dict:
    stored = (profile_data or {}).get(STORE_KEY)
    return stored if isinstance(stored, dict) else {}


def lookup(profile_data: dict) -> dict:
    """`{normalized question: answer}`, for the autofill."""
    return {key: entry["answer"] for key, entry in entries(profile_data).items()
            if isinstance(entry, dict) and entry.get("answer")}


def remember(profile_data: dict, answers: list) -> tuple[dict, int]:
    """The profile data with these answers kept, and how many were."""
    kept = dict(entries(profile_data))
    saved = 0
    now = datetime.now(timezone.utc).isoformat()
    for item in answers if isinstance(answers, list) else []:
        if not isinstance(item, dict):
            continue
        question = " ".join(str(item.get("question") or "").split())[:MAX_QUESTION]
        answer = " ".join(str(item.get("answer") or "").split())
        key = normalize_question(question)
        if not key or not answer or len(answer) > MAX_ANSWER:
            continue
        if _SENSITIVE.search(question) or _SENSITIVE.search(answer):
            continue
        kept[key] = {"question": question, "answer": answer, "at": now}
        saved += 1
    if len(kept) > MAX_ANSWERS:
        newest = sorted(kept.items(), key=lambda kv: kv[1].get("at") or "", reverse=True)
        kept = dict(newest[:MAX_ANSWERS])
    return {**(profile_data or {}), STORE_KEY: kept}, saved


def forget(profile_data: dict, key: str) -> dict:
    kept = {k: v for k, v in entries(profile_data).items() if k != key}
    return {**(profile_data or {}), STORE_KEY: kept}
