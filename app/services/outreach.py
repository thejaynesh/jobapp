"""
Outreach: who to write to, what to say, and what happens next.

The pipeline is deliberately three separable steps, because each fails for its
own reasons and none of them should take the others down:

  1. discover_contacts  — resolve the employer's domain, then collect people
                          from the posting, Hunter, and LinkedIn (see
                          services.contact_finder)
  2. draft_message      — write one message for one contact on one channel,
                          grounded in the profile and the job, with a usable
                          template fallback when no LLM answers
  3. the sequence       — a sent message comes due for a follow-up after
                          OUTREACH_FOLLOWUP_DAYS; a reply ends the sequence

Nothing here sends anything. Delivery lives in services.outreach_sender and
only ever runs from an explicit user action.
"""

import json
import logging
import re
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.llm.providers import collect_llm_log, generation_chat, start_llm_log
from app.models.outreach import (
    CLOSED_MESSAGE_STATUSES, Contact, MESSAGE_CHANNELS, MESSAGE_KINDS,
    MESSAGE_STATUSES, OutreachMessage,
)
from app.services.company_domain import company_key, extract_domain, resolve_company_domain
from app.services.contact_finder import (
    contact_score, contacts_from_description, find_email, find_linkedin_contact,
    find_linkedin_contacts, guess_emails, hunter_contacts, hunter_domain_search,
    hunter_email_finder, split_name, verify_email,
)
from app.services.github_contacts import github_contacts
from app.services.linkedin_links import company_links, contact_link
from app.services.team_pages import team_page_contacts

logger = logging.getLogger(__name__)

# Re-exported so callers have one import for the whole feature.
__all__ = [
    "extract_domain", "find_email", "find_linkedin_contact", "draft_outreach_message",
    "run_outreach", "discover_contacts", "draft_message", "regenerate_message",
    "compose_message", "mark_sent", "mark_replied", "set_message_status",
    "due_follow_ups", "draft_due_follow_ups", "outreach_stats", "channel_spec",
    "prior_conversations", "discovery_stale", "search_links", "contact_message_link",
    "outreach_priority", "default_kind", "add_business_days",
]


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------

# LinkedIn caps connection notes at 300 characters and silently truncates, so
# the budget here leaves room rather than finding out in the UI.
CHANNEL_SPECS: dict[str, dict] = {
    "email": {
        "label": "Email",
        "has_subject": True,
        "max_chars": 1600,
        # 50-125 words is where measured reply rates peak; longer reads as a
        # cover letter nobody asked for.
        "target_words": (60, 125),
        "guidance": (
            "A short email. Three short paragraphs at most, each one or two "
            "sentences. No bullet points."
        ),
    },
    "linkedin": {
        "label": "LinkedIn message",
        "has_subject": False,
        "max_chars": 900,
        "target_words": (60, 110),
        "guidance": "A LinkedIn direct message. Conversational, no salutation block, no signature.",
    },
    "linkedin_note": {
        "label": "LinkedIn connection note",
        "has_subject": False,
        "max_chars": 280,
        "target_words": (25, 45),
        "guidance": (
            "A LinkedIn connection request note. HARD LIMIT 280 characters including "
            "spaces — count them. One or two sentences, no greeting line, no signature."
        ),
    },
    "twitter": {
        "label": "X / Twitter DM",
        "has_subject": False,
        "max_chars": 275,
        "target_words": (25, 45),
        "guidance": "A direct message on X. Very short and casual, no signature.",
    },
}

TONES = {
    "warm": "Warm and human, but not chummy. Contractions are fine.",
    "formal": "Professional and restrained. No contractions, no exclamation marks.",
    "concise": "As short as it can be while still specific. Cut every optional word.",
    "enthusiastic": "Genuinely keen about the work, without gushing or flattery.",
}

KIND_GUIDANCE = {
    "initial": (
        "First contact. Say why this person specifically, name the role, give one "
        "concrete reason the candidate fits, and close with a single easy ask."
    ),
    "follow_up": (
        "A follow-up on an earlier message that got no reply. Reference the earlier "
        "note in half a sentence, do NOT repeat its content or its examples, add one "
        "new piece of value, and make it trivially easy to ignore or answer. Never "
        "guilt-trip or imply they were rude not to reply."
    ),
    "referral_request": (
        "Asking someone already at the company to refer the candidate — the highest "
        "converting thing they can do, and the biggest thing to ask for. A referral is "
        "a reputation bet: they are putting their name on a stranger. So earn it in "
        "three sentences. Lead with the specific, checkable reason the candidate can "
        "do this job. Say plainly that you are asking for a referral for the named "
        "role. Make saying yes almost free — offer to send a short blurb they can "
        "paste and the resume — and make saying no completely painless, explicitly. "
        "Never imply they owe a reply, and never call it a 'quick favour'."
    ),
    "thank_you": (
        "A thank-you after a conversation or interview. Reference something specific "
        "that was discussed, reinforce one point of fit, keep it short and do not "
        "re-pitch the whole application."
    ),
    "reconnect": (
        "Re-opening a conversation that went quiet a while ago. Assume they have "
        "forgotten the details, re-introduce briefly, and give a reason to talk now."
    ),
}

# Phrases that mark a message as mass-produced. Same idea as the cover letter's
# ban list — these are the tells a recruiter skims past.
BANNED_PHRASES = [
    "I hope this message finds you well",
    "I hope this email finds you well",
    "I hope you are doing well",
    "I wanted to reach out",
    "I am reaching out to you",
    "To whom it may concern",
    "perfect fit",
    "passionate about",
    "results-driven",
    "team player",
    "wealth of experience",
    "I would be a great asset",
    "please find attached my resume",
    "at your earliest convenience",
    "thank you for your time and consideration",
]

# Anything the model left for a human to fill in. A message with one of these in
# it is worse than no message, because it looks sent by mistake.
_PLACEHOLDER_RE = re.compile(r"\[[^\]\n]{1,40}\]|\{\{[^}\n]{1,40}\}\}|<[A-Za-z ]{1,30}>")
_SUBJECT_LINE_RE = re.compile(r"^\s*subject\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$")

MAX_SUBJECT_CHARS = 78


def channel_spec(channel: str) -> dict:
    return CHANNEL_SPECS.get(channel) or CHANNEL_SPECS["email"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


# Discovery's own soft time limit is five minutes; past double that, the worker
# is not coming back (OOM-killed, restarted mid-task) and the status is a lie.
DISCOVERY_TIMEOUT = timedelta(minutes=10)


def discovery_stale(application) -> bool:
    """
    Whether an in-progress search has been running impossibly long.

    The panel polls itself while status is "discovering". Without this it would
    poll forever for a worker that died, and the button to retry would stay
    disabled — so the one thing that could fix it is the one thing you can't do.
    """
    started = getattr(application, "outreach_checked_at", None)
    if getattr(application, "outreach_status", "") != "discovering":
        return False
    return started is None or _now() - started > DISCOVERY_TIMEOUT


# ---------------------------------------------------------------------------
# Profile helpers (the stored profile has grown, so read both shapes)
# ---------------------------------------------------------------------------

def _personal(profile_data: dict) -> dict:
    return profile_data.get("personal") or {}


def candidate_name(profile_data: dict) -> str:
    return _personal(profile_data).get("name") or profile_data.get("name") or "Candidate"


def candidate_email(profile_data: dict) -> str:
    return _personal(profile_data).get("email") or profile_data.get("email") or ""


def _skills_flat(profile_data: dict) -> list[str]:
    skills = profile_data.get("skills") or {}
    if isinstance(skills, list):
        return [str(s) for s in skills]
    return [s for items in skills.values() for s in (items or [])]


def _summary(profile_data: dict) -> str:
    return (profile_data.get("narrative") or {}).get("summary", "")


def _links(profile_data: dict) -> str:
    personal = _personal(profile_data)
    parts = [personal.get(key) for key in ("linkedin", "github", "website")]
    return " | ".join(p for p in parts if p)


def _evidence_lines(profile_data: dict, limit: int = 3) -> str:
    """
    The concrete accomplishments a message is allowed to cite.

    Same grounding rule as the cover letter: if it isn't in here, the model may
    not claim it.
    """
    lines: list[str] = []
    for exp in (profile_data.get("experience") or [])[:2]:
        role = exp.get("role") or exp.get("title") or ""
        lines.append(f"- {role} at {exp.get('company', '')}")
        lines.extend(f"    * {b}" for b in (exp.get("bullets") or [])[:limit])
    for proj in (profile_data.get("projects") or [])[:2]:
        lines.append(f"- Project {proj.get('name', '')}: {proj.get('description', '')}")
        lines.extend(f"    * {b}" for b in (proj.get("bullets") or [])[:2])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------

def _strip_fences(raw: str) -> str:
    return _JSON_FENCE_RE.sub("", (raw or "").strip()).strip()


def scrub(text: str, candidate: str = "") -> str:
    """
    Tidy raw model output into something sendable.

    Drops a leaked "Subject:" line, unfilled placeholders, and the surrounding
    quotes models like to wrap a message in. Placeholders are removed rather
    than left, because "[Your Name]" in a sent email is unrecoverable.
    """
    text = _strip_fences(text)
    text = _SUBJECT_LINE_RE.sub("", text, count=1)
    if candidate:
        # A signature the model guessed at is replaced with the real name.
        text = _PLACEHOLDER_RE.sub(
            lambda m: candidate if "name" in m.group().lower() else "", text
        )
    else:
        text = _PLACEHOLDER_RE.sub("", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip().strip('"').strip()


def enforce_limit(text: str, channel: str) -> str:
    """
    Cut a message to the channel's limit at a sentence boundary.

    LinkedIn truncates mid-word without telling anyone, so a message that ends
    on a full stop one sentence early beats one that ends on "I'd lo".
    """
    limit = channel_spec(channel)["max_chars"]
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    window = text[:limit]
    cut = max(window.rfind(". "), window.rfind("! "), window.rfind("? "), window.rfind("\n"))
    trimmed = window[:cut + 1].strip() if cut > limit * 0.5 else window.rsplit(" ", 1)[0].strip()
    logger.info("outreach: trimmed a %s message from %d to %d chars",
                channel, len(text), len(trimmed))
    return trimmed


def _thread_context(previous: list, limit: int = 2) -> str:
    lines = []
    for msg in (previous or [])[-limit:]:
        when = msg.sent_at.strftime("%b %d") if getattr(msg, "sent_at", None) else "unsent"
        lines.append(f"[{msg.kind}, {when}] {(msg.body or '')[:400]}")
    return "\n\n".join(lines)


def build_messages(
    profile_data: dict,
    contact: dict,
    job: dict,
    channel: str,
    kind: str,
    tone: str,
    feedback: str | None = None,
    thread: str = "",
) -> list[dict]:
    """The prompt for one message. Split out so it can be inspected and tested."""
    spec = channel_spec(channel)
    low, high = spec["target_words"]
    name = candidate_name(profile_data)
    contact_name = contact.get("name") or ""
    contact_title = contact.get("title") or ""
    contact_role = contact.get("role") or "unknown"

    salutation_rule = (
        f"Address them as {contact_name.split()[0]}." if contact_name
        else "You do NOT know their name — open without a salutation, or with a neutral "
             "one. Never write 'Dear Hiring Manager' or 'To whom it may concern'."
    )

    system = (
        "You write outreach messages for a job seeker. They are read in five seconds "
        "by a busy person, so every sentence must earn its place.\n"
        f"Channel: {spec['label']}. {spec['guidance']}\n"
        f"Length: {low}-{high} words, and never more than {spec['max_chars']} characters.\n"
        f"Purpose: {KIND_GUIDANCE.get(kind, KIND_GUIDANCE['initial'])}\n"
        f"Tone: {TONES.get(tone, TONES['warm'])}\n"
        "Hard rules:\n"
        f"- {salutation_rule}\n"
        "- Use ONLY the accomplishments in the evidence list. Never invent an employer, "
        "a metric, a technology, or a mutual connection.\n"
        "- Be specific about this company and this role. If you have nothing specific "
        "to say about the company, be specific about the work instead — never fill the "
        "gap with flattery.\n"
        f"- Never use these phrases or close variants: {'; '.join(BANNED_PHRASES)}.\n"
        "- Never leave a placeholder to fill in. Write the finished text.\n"
        "- One clear ask, at the end.\n"
        + (
            "Return ONLY a JSON object: {\"subject\": \"...\", \"body\": \"...\"}.\n"
            "The subject decides whether any of this is read. Six to ten words, "
            "written like one person emailing another. Use one of these shapes:\n"
            "  - a specific question about their team or work "
            "('Question about how your platform team is split')\n"
            "  - a concrete point of overlap "
            "('Northeastern grad working on the same problem you are')\n"
            "  - the role plus a real reason, never the role alone\n"
            "Never write a subject shaped like 'Job Title - Candidate Name', and never "
            "use 'Application', 'Opportunity', 'Resume', or 'Following up' — those read "
            "as an automated ATS mail and get deleted unopened. "
            f"At most {MAX_SUBJECT_CHARS} characters. No markdown."
            if spec["has_subject"] else
            "Return ONLY the message text. No subject line, no markdown, no commentary."
        )
    )

    user = (
        f"Candidate: {name}\n"
        f"Candidate summary: {_summary(profile_data)}\n"
        f"Top skills: {', '.join(_skills_flat(profile_data)[:8])}\n"
        + (f"Links: {_links(profile_data)}\n" if _links(profile_data) else "")
        + f"\nEvidence (the ONLY accomplishments you may cite):\n{_evidence_lines(profile_data)}\n"
        + f"\nRecipient: {contact_name or 'unknown name'}"
        + (f", {contact_title}" if contact_title else "")
        + f" at {job.get('company', '')} (relationship to the role: {contact_role})\n"
        + f"Target role: {job.get('title', '')} at {job.get('company', '')}"
        + (f" ({job.get('location')})" if job.get("location") else "")
        + "\n"
        + (f"Why this role fits the candidate: {job['match_reasoning']}\n"
           if job.get("match_reasoning") else "")
        + (f"Skills the role and candidate share: {', '.join(job['matched_skills'][:8])}\n"
           if job.get("matched_skills") else "")
        + (f"Job description (excerpt):\n{(job.get('description') or '')[:1500]}\n"
           if job.get("description") else "")
        + (f"\nEarlier messages in this thread (do not repeat them):\n{thread}\n" if thread else "")
        + (f"\nUser feedback on the previous draft (must address): {feedback}\n" if feedback else "")
    )

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _fallback_subject(job: dict, kind: str) -> str:
    """
    A subject line for when the model didn't supply one.

    Deliberately not "Backend Engineer — Jane Doe": that shape is what every
    ATS autoresponder uses, so it gets filed unread. A question about the team
    reads as a person and gets opened.
    """
    company = job.get("company") or "your team"
    title = job.get("title") or "the role"
    if kind == "referral_request":
        return f"Question about {company} — and a small ask"[:MAX_SUBJECT_CHARS]
    if kind == "thank_you":
        return f"Thanks for the conversation about {title}"[:MAX_SUBJECT_CHARS]
    if kind in ("follow_up", "reconnect"):
        return f"Circling back on {title} at {company}"[:MAX_SUBJECT_CHARS]
    return f"Question about the {title} role at {company}"[:MAX_SUBJECT_CHARS]


def fallback_message(profile_data: dict, contact: dict, job: dict, channel: str, kind: str) -> dict:
    """
    A usable message written without an LLM.

    Reached when every provider is down or unconfigured. It is plainer than a
    generated one, but it is specific, correctly addressed, and sendable — which
    beats an error where the draft should be.
    """
    spec = channel_spec(channel)
    name = candidate_name(profile_data)
    greeting_name = (contact.get("name") or "").split()[0] if contact.get("name") else ""
    hello = f"Hi {greeting_name}," if greeting_name else "Hi,"
    title = job.get("title") or "the role"
    company = job.get("company") or "your team"
    skills = _skills_flat(profile_data)[:3]
    skill_text = ", ".join(skills) if skills else "software engineering"

    if kind == "follow_up":
        core = (
            f"I wrote last week about the {title} role at {company} — I know inboxes get "
            f"busy. My background is in {skill_text}, and I would still love a few minutes "
            "to hear how the team is thinking about the role."
        )
    elif kind == "referral_request":
        core = (
            f"I am applying for the {title} role at {company} and wondered whether you would "
            f"be open to referring me. My background is in {skill_text}; happy to send a short "
            "blurb and my resume so it takes you a minute at most."
        )
    elif kind == "thank_you":
        core = (
            f"Thank you for the time today talking about the {title} role. The conversation "
            f"made me more interested, not less, and my work in {skill_text} lines up closely "
            "with what you described."
        )
    else:
        core = (
            f"I came across the {title} role at {company} and it lines up closely with what I "
            f"have been building — mostly {skill_text}. I would like to put my name in front "
            "of you rather than only through the form."
        )

    if kind == "referral_request":
        ask = ("No pressure at all if it's not something you'd do for someone you "
               "haven't worked with — happy to send a short blurb and my resume if it is.")
    else:
        ask = "Would you be open to a short conversation?"

    if spec["has_subject"]:
        body = f"{hello}\n\n{core}\n\n{ask}\n\nThanks,\n{name}"
    else:
        body = f"{hello} {core} {ask}"

    return {
        "subject": _fallback_subject(job, kind) if spec["has_subject"] else None,
        "body": enforce_limit(body, channel),
        "generated_by": None,
    }


def compose_message(
    profile_data: dict,
    contact: dict,
    job: dict,
    channel: str = "email",
    kind: str = "initial",
    tone: str = "warm",
    feedback: str | None = None,
    thread: str = "",
) -> dict:
    """
    Write one message. Returns {"subject", "body", "generated_by"}.

    `contact` and `job` are plain dicts so this stays testable without a
    database and reusable for a contact that has not been saved yet.
    """
    spec = channel_spec(channel)
    messages = build_messages(profile_data, contact, job, channel, kind, tone, feedback, thread)

    start_llm_log()
    try:
        raw = generation_chat(
            messages=messages,
            api_key=settings.NVIDIA_NIM_API_KEY,
            base_url=settings.NVIDIA_NIM_BASE_URL,
            model=settings.NVIDIA_NIM_MODEL,
            temperature=0.7,
            max_tokens=800,
        )
    except Exception as exc:
        logger.error("compose_message: no provider produced a draft: %s", exc)
        collect_llm_log()
        return fallback_message(profile_data, contact, job, channel, kind)

    generated_by = ", ".join(collect_llm_log()) or None
    subject, body = _parse_output(raw, spec["has_subject"])
    body = enforce_limit(scrub(body, candidate_name(profile_data)), channel)

    if len(body) < 40:
        logger.warning("compose_message: draft was too short to use (%d chars)", len(body))
        return fallback_message(profile_data, contact, job, channel, kind)

    if spec["has_subject"] and not subject:
        subject = _fallback_subject(job, kind)
    if subject:
        subject = scrub(subject, candidate_name(profile_data)).replace("\n", " ")[:MAX_SUBJECT_CHARS]

    return {"subject": subject or None, "body": body, "generated_by": generated_by}


def _parse_output(raw: str, has_subject: bool) -> tuple[str, str]:
    """Pull (subject, body) out of a reply that may or may not be the JSON we asked for."""
    text = _strip_fences(raw)
    if not has_subject:
        return "", text
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return str(parsed.get("subject") or "").strip(), str(parsed.get("body") or "").strip()
    except Exception:
        pass
    # Not JSON — accept a plain draft with a "Subject:" first line.
    match = _SUBJECT_LINE_RE.search(text)
    subject = match.group(1).strip() if match else ""
    return subject, text


def draft_outreach_message(
    profile_data: dict,
    contact_name: str,
    contact_title: str,
    job_title: str,
    company: str,
    api_key: str,
    base_url: str,
    model: str,
) -> str:
    """
    Message text for a contact, from loose fields rather than model objects.

    The narrow entry point kept for callers (and the JSON API) that only have
    these five facts to work with.
    """
    result = compose_message(
        profile_data,
        {"name": contact_name, "title": contact_title},
        {"title": job_title, "company": company},
        channel="linkedin",
        kind="initial",
    )
    return result["body"]


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _job_dict(job) -> dict:
    return {
        "title": getattr(job, "title", "") or "",
        "company": getattr(job, "company", "") or "",
        "location": getattr(job, "location", "") or "",
        "description": getattr(job, "description", "") or "",
        "match_reasoning": getattr(job, "llm_reasoning", "") or "",
        "matched_skills": list(getattr(job, "matched_skills", None) or []),
    }


def _contact_dict(contact: Contact) -> dict:
    return {
        "name": contact.name,
        "title": contact.title,
        "role": contact.role,
        "department": contact.department,
        "email": contact.email,
    }


def _dedupe(candidates: list[dict]) -> list[dict]:
    """
    Merge the same person arriving from two sources.

    Keyed on the address where there is one, otherwise on the name — LinkedIn
    gives a name and no address, Hunter often the reverse, and the merged record
    is worth more than either.
    """
    merged: dict[str, dict] = {}
    for candidate in candidates:
        email = (candidate.get("email") or "").lower()
        name = (candidate.get("name") or "").lower().strip()
        key = f"email:{email}" if email else (f"name:{name}" if name else None)
        if key is None:
            continue
        if key not in merged:
            merged[key] = dict(candidate)
            continue
        existing = merged[key]
        for field, value in candidate.items():
            if value and not existing.get(field):
                existing[field] = value

    # A LinkedIn record with a name can now be folded into a Hunter record for
    # the same name that also has an address.
    by_name: dict[str, dict] = {}
    result: list[dict] = []
    for key, candidate in merged.items():
        name = (candidate.get("name") or "").lower().strip()
        if name and name in by_name:
            for field, value in candidate.items():
                if value and not by_name[name].get(field):
                    by_name[name][field] = value
            continue
        if name:
            by_name[name] = candidate
        result.append(candidate)
    return result


def _attach_guessed_emails(candidates: list[dict], domain: str, pattern: str, hunter_key: str) -> None:
    """Give contacts we only have a name for a best-effort address."""
    if not domain:
        return
    for candidate in candidates:
        if candidate.get("email"):
            continue
        first = candidate.get("first_name") or split_name(candidate.get("name") or "")[0]
        last = candidate.get("last_name") or split_name(candidate.get("name") or "")[1]
        if not first:
            continue
        if hunter_key:
            found = hunter_email_finder(domain, first, last, hunter_key)
            if found.get("email"):
                candidate["email"] = found["email"]
                candidate["email_status"] = "unverified"
                candidate["email_confidence"] = found.get("score") or 50
                continue
        if not settings.OUTREACH_GUESS_EMAILS:
            continue
        guesses = guess_emails(first, last, domain, pattern)
        if guesses:
            candidate["email"] = guesses[0]
            candidate["email_status"] = "guessed"
            # A pattern Hunter observed at this company is worth much more than
            # first.last being the commonest shape on the internet.
            candidate["email_confidence"] = 60 if pattern else 30
            candidate["alternate_emails"] = guesses[1:4]


def upsert_contact(db, application, data: dict) -> Contact:
    """
    Store a discovered person, reusing this application's existing row for them.

    Fields are only ever filled in, never blanked: a later, thinner source must
    not erase a title or a LinkedIn URL an earlier one found. Anything the user
    typed by hand outranks discovery and is left alone.
    """
    company = getattr(application.job, "company", "") or ""
    key = company_key(company)
    email = (data.get("email") or "").strip().lower() or None

    existing = None
    if email:
        existing = (
            db.query(Contact)
            .filter(Contact.application_id == application.id, Contact.email == email)
            .first()
        )
    if existing is None and data.get("name"):
        existing = (
            db.query(Contact)
            .filter(
                Contact.application_id == application.id,
                Contact.name == data["name"],
            )
            .first()
        )

    if existing is None:
        contact = Contact(
            application_id=application.id,
            company=company,
            company_key=key,
            email=email,
            source=data.get("source") or "manual",
            role=data.get("role") or "unknown",
            email_status=data.get("email_status") or ("unknown" if not email else "unverified"),
            email_confidence=int(data.get("email_confidence") or 0),
            alternate_emails=list(data.get("alternate_emails") or []),
        )
        db.add(contact)
    else:
        contact = existing
        if contact.source == "manual" and data.get("source") != "manual":
            # Hand-entered records are the user's; discovery may top them up but
            # not restate where they came from.
            pass
        elif data.get("email_confidence", 0) > (contact.email_confidence or 0):
            contact.email_status = data.get("email_status") or contact.email_status
            contact.email_confidence = int(data.get("email_confidence") or 0)
            contact.source = data.get("source") or contact.source

    for field in ("name", "first_name", "last_name", "title", "department",
                  "linkedin_url", "profile_url", "twitter", "phone", "domain"):
        value = data.get(field)
        if value and not getattr(contact, field, None):
            setattr(contact, field, value)
    if contact.role in (None, "", "unknown") and data.get("role"):
        contact.role = data["role"]
    if email and not contact.email:
        contact.email = email
        contact.email_status = data.get("email_status") or "unverified"
        contact.email_confidence = int(data.get("email_confidence") or 0)
    return contact


def discover_contacts(
    db,
    application,
    use_linkedin: bool | None = None,
    verify: bool | None = None,
    max_contacts: int | None = None,
) -> list[Contact]:
    """
    Find people to write to about one application and store them.

    Every source is optional. With no Hunter key, no LinkedIn cookie, and no
    address in the posting, this still returns the company's careers mailbox
    rather than nothing at all — which is what a person would do by hand.
    """
    job = application.job
    hunter_key = settings.HUNTER_IO_API_KEY
    use_linkedin = settings.OUTREACH_USE_LINKEDIN if use_linkedin is None else use_linkedin
    verify = settings.OUTREACH_VERIFY_EMAILS if verify is None else verify
    max_contacts = max_contacts or settings.OUTREACH_MAX_CONTACTS_PER_APP

    domain, domain_source = resolve_company_domain(
        job.company,
        url=job.url or "",
        apply_url=getattr(job, "apply_url", "") or "",
        description=job.description or "",
    )
    logger.info(
        "discover_contacts %s: %s -> domain %r (from %s)",
        application.id, job.company, domain, domain_source or "nothing",
    )

    candidates: list[dict] = []
    pattern = ""

    # Ordered by how much each source's output can be trusted, because _dedupe
    # keeps the first non-empty value for every field. The posting's own
    # addresses come first: free, deliberate, and aimed at applicants.
    candidates.extend(contacts_from_description(job.description or "", domain))

    if domain and hunter_key:
        data = hunter_domain_search(domain, hunter_key, limit=20)
        pattern = data.get("pattern") or ""
        candidates.extend(hunter_contacts(domain, hunter_key, limit=10, data=data))

    # The company's own site: LinkedIn profile links and published addresses.
    if settings.OUTREACH_USE_TEAM_PAGES and domain:
        try:
            candidates.extend(team_page_contacts(domain, limit=max_contacts))
        except Exception as exc:
            logger.error("discover_contacts: team pages failed for %s: %s", domain, exc)

    # Public GitHub org members — named engineers, which is who referrals come from.
    if settings.OUTREACH_USE_GITHUB and settings.GITHUB_TOKEN:
        try:
            candidates.extend(
                github_contacts(job.company, domain, settings.GITHUB_TOKEN, limit=max_contacts)
            )
        except Exception as exc:
            logger.error("discover_contacts: github failed for %s: %s", job.company, exc)

    if use_linkedin and settings.LINKEDIN_SESSION_COOKIE:
        titles = [t.strip() for t in settings.OUTREACH_TARGET_TITLES.split(",") if t.strip()]
        # Capped hard: every extra authenticated search from a datacenter IP is
        # another chance at an account restriction, and one good query beats five.
        for query in titles[:max(1, settings.OUTREACH_LINKEDIN_MAX_SEARCHES)]:
            candidates.extend(
                find_linkedin_contacts(
                    job.company, [query], settings.LINKEDIN_SESSION_COOKIE, limit=3
                )
            )

    candidates = _dedupe(candidates)
    _attach_guessed_emails(candidates, domain, pattern, hunter_key)

    if not candidates and domain and settings.OUTREACH_INCLUDE_GENERIC_MAILBOX:
        # Off by default, and deliberately so. A careers@ mailbox is the resume
        # black hole with extra steps: it routes into the same ATS the form
        # does, so a message there converts like a cold application (0.1-2%)
        # rather than like reaching a person. Finding nobody is the honest
        # answer, and the panel says so and points at the LinkedIn searches,
        # which is the path that actually works from here.
        candidates = [{
            "name": None, "title": None, "role": "generic", "email": f"careers@{domain}",
            "email_status": "guessed", "email_confidence": 25, "source": "pattern",
        }]

    if verify and hunter_key:
        for candidate in candidates[:max_contacts]:
            if candidate.get("email") and candidate.get("email_status") != "verified":
                result = verify_email(candidate["email"], hunter_key)
                if result:
                    candidate["email_status"] = result["status"]
                    candidate["email_confidence"] = max(
                        int(candidate.get("email_confidence") or 0), result["confidence"]
                    )

    candidates.sort(key=contact_score, reverse=True)
    # A GitHub profile or an X handle is a way to reach someone even with no
    # address, so "reachable" is broader than email plus LinkedIn.
    kept = [
        c for c in candidates
        if c.get("email") or c.get("linkedin_url") or c.get("profile_url") or c.get("twitter")
    ][:max_contacts]

    stored = [upsert_contact(db, application, c | {"domain": c.get("domain") or domain})
              for c in kept]
    application.outreach_checked_at = _now()
    db.commit()
    logger.info("discover_contacts %s: stored %d contact(s)", application.id, len(stored))
    return stored


# ---------------------------------------------------------------------------
# Drafting against the database
# ---------------------------------------------------------------------------

def _profile_data(db) -> dict:
    from app.models.profile import Profile

    profile = db.query(Profile).first()
    return (profile.data if profile else {}) or {}


def default_channel(contact: Contact) -> str:
    """Email when we can reach them, LinkedIn when all we have is a profile."""
    if contact.email:
        return "email"
    if contact.linkedin_url:
        return "linkedin"
    return "email"


def default_kind(contact: Contact) -> str:
    """
    What to ask this person for.

    A peer engineer is the referral path, which converts an order of magnitude
    better than anything else available, so that is what we ask them for rather
    than opening with a generic introduction. A hiring manager or recruiter owns
    the req itself — there is nothing to refer, so it is a direct approach.
    """
    if (contact.role or "unknown") in ("engineer", "executive", "unknown"):
        return "referral_request"
    return "initial"


def next_step(contact: Contact) -> int:
    steps = [m.sequence_step for m in (contact.messages or [])]
    return (max(steps) + 1) if steps else 1


def draft_message(
    db,
    contact: Contact,
    channel: str | None = None,
    kind: str | None = None,
    tone: str = "warm",
    feedback: str | None = None,
    application=None,
) -> OutreachMessage:
    """Write and store one message for a contact."""
    channel = channel if channel in MESSAGE_CHANNELS else default_channel(contact)
    kind = kind if kind in MESSAGE_KINDS else default_kind(contact)
    application = application or contact.application
    job = application.job if application else None

    prior = [m for m in (contact.messages or []) if m.status in ("sent", "replied")]
    result = compose_message(
        _profile_data(db),
        _contact_dict(contact),
        _job_dict(job) if job else {"title": "", "company": contact.company},
        channel=channel,
        kind=kind,
        tone=tone,
        feedback=feedback,
        thread=_thread_context(prior),
    )

    message = OutreachMessage(
        contact_id=contact.id,
        application_id=application.id if application else None,
        channel=channel,
        kind=kind,
        tone=tone,
        sequence_step=next_step(contact),
        subject=result["subject"],
        body=result["body"],
        generated_by=result["generated_by"],
        feedback=feedback,
        status="draft",
    )
    db.add(message)
    db.commit()
    return message


def regenerate_message(db, message: OutreachMessage, feedback: str | None = None) -> OutreachMessage:
    """
    Rewrite a draft in place, keeping its position in the sequence.

    Only drafts are rewritten — a sent message is a record of what was actually
    said and must not change under the user.
    """
    if message.status in ("sent", "replied", "bounced"):
        raise ValueError("A message that has already been sent cannot be rewritten.")

    contact = message.contact
    application = message.application or contact.application
    job = application.job if application else None
    prior = [
        m for m in (contact.messages or [])
        if m.id != message.id and m.status in ("sent", "replied")
    ]

    result = compose_message(
        _profile_data(db),
        _contact_dict(contact),
        _job_dict(job) if job else {"title": "", "company": contact.company},
        channel=message.channel,
        kind=message.kind,
        tone=message.tone,
        feedback=feedback or message.feedback,
        thread=_thread_context(prior),
    )
    message.subject = result["subject"]
    message.body = result["body"]
    message.generated_by = result["generated_by"]
    message.feedback = feedback or message.feedback
    message.edited = False
    message.status = "draft"
    db.commit()
    return message


# ---------------------------------------------------------------------------
# The follow-up sequence
# ---------------------------------------------------------------------------

def add_business_days(start: datetime, days: int) -> datetime:
    """
    Advance a date by working days, skipping weekends.

    Follow-up intervals are quoted in business days wherever they are measured,
    and the difference isn't cosmetic: three calendar days after a Thursday send
    lands on Sunday, where the message is buried by Monday morning.
    """
    result = start
    remaining = max(0, days)
    while remaining > 0:
        result += timedelta(days=1)
        if result.weekday() < 5:
            remaining -= 1
    return result


def followup_days() -> list[int]:
    """Business days after sending that each successive follow-up comes due."""
    days: list[int] = []
    for part in (settings.OUTREACH_FOLLOWUP_DAYS or "").split(","):
        part = part.strip()
        if part.isdigit() and int(part) > 0:
            days.append(int(part))
    return days


def max_sequence_steps() -> int:
    """The initial message plus one step per configured interval."""
    return len(followup_days()) + 1


def mark_sent(db, message: OutreachMessage, when: datetime | None = None) -> OutreachMessage:
    """
    Record that a message went out, and queue its follow-up.

    Used both by the SMTP sender and by the "I sent this myself" button, so the
    sequence works identically whether or not sending is configured.
    """
    when = when or _now()
    message.status = "sent"
    message.sent_at = when
    message.send_error = None

    days = followup_days()
    index = message.sequence_step - 1
    if index < len(days):
        message.follow_up_due_at = add_business_days(when, days[index])
    else:
        message.follow_up_due_at = None
    db.commit()
    return message


def mark_replied(db, message: OutreachMessage, when: datetime | None = None) -> OutreachMessage:
    """
    Record a reply and stop chasing.

    A reply ends the whole sequence, not just this message: pending follow-up
    drafts for the same contact are dropped, since sending one after they have
    already answered is the worst outcome the feature can produce.
    """
    message.status = "replied"
    message.replied_at = when or _now()
    message.follow_up_due_at = None

    for other in list(message.contact.messages or []):
        if other.id == message.id:
            continue
        other.follow_up_due_at = None
        if other.status in ("draft", "approved") and other.kind == "follow_up":
            db.delete(other)
    db.commit()
    return message


def set_message_status(db, message: OutreachMessage, status: str) -> OutreachMessage:
    if status not in MESSAGE_STATUSES:
        raise ValueError(f"Unknown message status: {status}")
    if status == "sent":
        return mark_sent(db, message)
    if status == "replied":
        return mark_replied(db, message)
    message.status = status
    if status in CLOSED_MESSAGE_STATUSES:
        message.follow_up_due_at = None
    db.commit()
    return message


def due_follow_ups(db, now: datetime | None = None, limit: int = 50) -> list[OutreachMessage]:
    """Sent messages whose follow-up window has elapsed with no reply."""
    now = now or _now()
    return (
        db.query(OutreachMessage)
        .filter(
            OutreachMessage.status == "sent",
            OutreachMessage.replied_at.is_(None),
            OutreachMessage.follow_up_due_at.isnot(None),
            OutreachMessage.follow_up_due_at <= now,
        )
        .order_by(OutreachMessage.follow_up_due_at)
        .limit(limit)
        .all()
    )


def draft_due_follow_ups(db, limit: int = 25) -> list[OutreachMessage]:
    """
    Write the next message for every sequence that has come due.

    Drafts only — they land in the outreach page for review. The window is
    cleared whether or not drafting succeeded, so one contact whose draft keeps
    failing cannot be retried forever.
    """
    drafted: list[OutreachMessage] = []
    for message in due_follow_ups(db, limit=limit):
        contact = message.contact
        message.follow_up_due_at = None
        if contact is None or contact.archived:
            db.commit()
            continue
        if any(m.status == "replied" for m in (contact.messages or [])):
            db.commit()
            continue
        if next_step(contact) > max_sequence_steps():
            db.commit()
            continue
        try:
            drafted.append(
                draft_message(
                    db, contact,
                    channel=message.channel,
                    kind="follow_up",
                    tone=message.tone,
                    application=message.application,
                )
            )
        except Exception as exc:
            logger.error("draft_due_follow_ups: contact %s failed: %s", contact.id, exc)
            db.rollback()
    db.commit()
    logger.info("draft_due_follow_ups: drafted %d follow-up(s)", len(drafted))
    return drafted


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_outreach(db, application, draft: bool = True) -> list[Contact]:
    """
    Discover contacts for an application and draft an opening message for each.

    The one call a caller needs: the API endpoint, the Celery task, and the
    "Find contacts" button all land here.
    """
    if not settings.OUTREACH_ENABLED:
        logger.info("run_outreach: disabled by configuration")
        return []

    contacts = discover_contacts(db, application)
    if not draft:
        return contacts

    for contact in contacts:
        if contact.messages:
            continue  # already has a thread — don't start a second one
        try:
            draft_message(db, contact, application=application)
        except Exception as exc:
            logger.error("run_outreach: draft for contact %s failed: %s", contact.id, exc)
            db.rollback()
    return contacts


def search_links(db, application) -> list[dict]:
    """
    Ready-made LinkedIn searches for this employer.

    The reliable half of LinkedIn outreach: the server never calls LinkedIn, the
    user clicks through already logged in, and there is nothing to block or get
    an account restricted for. Alumni angles come first when the profile has an
    education history.
    """
    job = getattr(application, "job", None)
    if job is None:
        return []
    return company_links(
        job.company,
        profile_data=_profile_data(db),
        description=job.description or "",
        url=getattr(job, "apply_url", "") or job.url or "",
    )


def contact_message_link(contact: Contact) -> str:
    """The best LinkedIn URL for one person — their profile, or a search for them."""
    return contact_link(contact)


# Outreach that works is a few dozen researched messages, not hundreds of
# generic ones — so the scarce resource is the user's attention, and the job of
# this score is to spend it on the applications where a message can actually
# change the outcome.
PRIORITY_LABELS = {2: "Worth a message", 1: "Maybe", 0: "Skip"}


def outreach_priority(application, contacts: list[Contact] | None = None) -> dict:
    """
    Whether this application deserves the effort, and why.

    Returns {"level", "label", "reasons"}. The dominant signal is whether we
    found a real, named human: a message to a person converts; a message to a
    generic mailbox converts like the application form it feeds into.
    """
    contacts = [c for c in (contacts or []) if not c.archived]
    reasons: list[str] = []
    score = 0

    referrers = [c for c in contacts if c.role in ("engineer", "hiring_manager") and c.name]
    named = [c for c in contacts if c.name]
    if referrers:
        score += 3
        reasons.append(f"{len(referrers)} named person who could refer or decide")
    elif named:
        score += 2
        reasons.append(f"{len(named)} named contact")
    elif contacts:
        reasons.append("only a generic mailbox — converts like the application form")
    else:
        reasons.append("no contacts found yet")

    job = getattr(application, "job", None)
    match = getattr(job, "llm_score", None) if job else None
    if match is not None:
        if match >= 80:
            score += 2
            reasons.append(f"strong match ({match:.0f})")
        elif match >= 65:
            score += 1
            reasons.append(f"decent match ({match:.0f})")
        else:
            score -= 1
            reasons.append(f"weak match ({match:.0f})")

    posted = getattr(job, "posted_at", None) if job else None
    if posted is not None:
        try:
            age = (_now() - posted).days
        except TypeError:  # naive datetime from an older row
            age = None
        if age is not None:
            if age <= 7:
                score += 1
                reasons.append("posted this week — the req is still warm")
            elif age > 30:
                score -= 1
                reasons.append(f"posted {age} days ago")

    level = 2 if score >= 4 else (1 if score >= 2 else 0)
    return {"level": level, "label": PRIORITY_LABELS[level], "reasons": reasons}


def prior_conversations(db, contact: Contact, limit: int = 5) -> list[dict]:
    """
    Messages already sent to this person about other roles at the same company.

    Contacts are per-application, so the same recruiter legitimately appears
    twice — but writing to them a second time without knowing about the first is
    how outreach turns into spam. The panel shows this before you hit send.
    """
    if not contact.email:
        return []
    rows = (
        db.query(OutreachMessage)
        .join(Contact, OutreachMessage.contact_id == Contact.id)
        .filter(
            Contact.company_key == contact.company_key,
            Contact.email == contact.email,
            Contact.id != contact.id,
            OutreachMessage.sent_at.isnot(None),
        )
        .order_by(OutreachMessage.sent_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "title": (message.application.job.title if message.application else "another role"),
            "sent_at": message.sent_at,
            "status": message.status,
        }
        for message in rows
    ]


def outreach_stats(db) -> dict:
    """Counts for the outreach page header."""
    from sqlalchemy import func as sa_func

    rows = dict(
        db.query(OutreachMessage.status, sa_func.count(OutreachMessage.id))
        .group_by(OutreachMessage.status)
        .all()
    )
    sent = rows.get("sent", 0) + rows.get("replied", 0)
    replied = rows.get("replied", 0)
    return {
        "contacts": db.query(Contact).filter(Contact.archived.is_(False)).count(),
        "drafts": rows.get("draft", 0) + rows.get("approved", 0),
        "sent": sent,
        "replied": replied,
        "bounced": rows.get("bounced", 0),
        "reply_rate": round(replied / sent * 100) if sent else 0,
        "due": len(due_follow_ups(db, limit=200)),
    }
