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

import hashlib
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit, urlunsplit

STORE_KEY = "remembered_answers"
MAX_ANSWERS = 500
MAX_QUESTION = 300
MAX_ANSWER = 500
_VOLATILE = re.compile(r"available|availability|start|notice|salary|compensation|relocat|hybrid|remote|sponsor|authoriz", re.I)


def site_scope(url):
    """A versioned employer identity, or a single-page identity if uncertain.

    A locale or an ATS route such as /Recruiting is not an employer. Unknown
    URL layouts therefore reuse answers only on the exact page. Hashing that
    fallback also keeps query parameters out of the saved profile.
    """
    try:
        parsed = urlsplit(str(url or ""))
        if (parsed.scheme not in {"https", "http"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            return ""
        host = parsed.hostname.lower()
        parts = [part for part in parsed.path.split("/") if part]
        query = parse_qs(parsed.query)
        employer = None
        if re.fullmatch(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io", host):
            if len(parts) >= 2 and parts[0] == "embed" and parts[1] in {"job_app", "job_board"}:
                values = query.get("for", [])
                employer = values[0] if len(values) == 1 else None
            elif parts and parts[0] != "embed":
                employer = parts[0]
        elif host == "jobs.dayforcehcm.com":
            if len(parts) >= 2 and re.fullmatch(r"[a-z]{2}(?:-[a-z]{2})?", parts[0], re.I):
                employer = parts[1]
        elif host == "recruiting.paylocity.com":
            if len(parts) >= 4 and [p.lower() for p in parts[:2]] == ["recruiting", "jobs"]:
                route, identity = parts[2].lower(), parts[3]
                if route == "all":
                    employer = identity
                elif route in {"details", "apply"} and identity.isdigit():
                    # A posting URL has a job ID, not a company ID. Never
                    # reuse that answer for another posting on this host.
                    return f"v2:posting:{host}/{identity}"
        elif host == "ats.rippling.com":
            if parts[:3] == ["api", "v2", "board"]:
                parts = parts[3:]
            if parts and re.fullmatch(r"[a-z]{2}-[a-z]{2}", parts[0], re.I):
                parts = parts[1:]
            employer = parts[0] if parts else None
        elif host == "jobs.jobvite.com":
            if parts and parts[0] == "careers":
                parts = parts[1:]
            employer = parts[0] if parts else None
        elif host in {"jobs.lever.co", "jobs.eu.lever.co", "jobs.ashbyhq.com", "apply.workable.com",
                      "jobs.smartrecruiters.com",
                      "jobs.gem.com", "recruiting.ultipro.com", "recruiting2.ultipro.com"}:
            employer = parts[0] if parts else None
        elif any(host.endswith("." + domain) for domain in (
                "myworkdayjobs.com", "icims.com", "taleo.net", "recruitee.com",
                "bamboohr.com", "applytojob.com", "breezy.hr", "pinpointhq.com",
                "teamtailor.com", "jobs.personio.de", "jobs.personio.com")):
            return f"v2:employer:{host}"
        if (employer and re.fullmatch(r"[a-z0-9_.-]+", employer, re.I)
                and employer.lower() not in {"jobs", "job", "apply", "embed", "careers", "api"}):
            return f"v2:employer:{host}/{employer}"
        address = urlunsplit((parsed.scheme, parsed.netloc.lower(), parsed.path,
                              parsed.query, parsed.fragment))
        return "v2:page:" + hashlib.sha256(address.encode()).hexdigest()
    except ValueError:
        return ""

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


def lookup(profile_data: dict, site="", now=None) -> dict:
    """`{normalized question: answer}`, for the autofill."""
    from app.services.tunables import value
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=int(value(profile_data, "answer_expiry_days")))
    scope = site_scope(site)
    if site and not scope:
        return {}
    found = {}
    for key, entry in entries(profile_data).items():
        if not isinstance(entry, dict) or not entry.get("answer"):
            continue
        # Old host/first-path keys cannot prove an employer identity. Unscoped
        # answers are available only to unscoped callers, never a named site.
        if (entry.get("scope") or "") != scope:
            continue
        if _VOLATILE.search(entry.get("question") or key):
            try:
                at = datetime.fromisoformat(entry.get("at") or "")
                if at.tzinfo is None or at < cutoff:
                    continue
            except ValueError:
                continue
        question = normalize_question(entry.get("question") or key)
        found[question] = entry["answer"]
    return found


def remember(profile_data: dict, answers: list, site="") -> tuple[dict, int]:
    """The profile data with these answers kept, and how many were."""
    kept = dict(entries(profile_data))
    saved = 0
    now = datetime.now(timezone.utc).isoformat()
    scope = site_scope(site)
    if site and not scope:
        return {**(profile_data or {}), STORE_KEY: kept}, 0
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
        storage_key = key + (" @ " + scope if scope else "")
        kept[storage_key] = {"question": question, "answer": answer, "at": now, "scope": scope}
        saved += 1
    if len(kept) > MAX_ANSWERS:
        newest = sorted(kept.items(), key=lambda kv: kv[1].get("at") or "", reverse=True)
        kept = dict(newest[:MAX_ANSWERS])
    return {**(profile_data or {}), STORE_KEY: kept}, saved


def forget(profile_data: dict, key: str) -> dict:
    kept = {k: v for k, v in entries(profile_data).items() if k != key}
    return {**(profile_data or {}), STORE_KEY: kept}
