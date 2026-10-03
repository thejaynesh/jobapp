"""Deterministic, review-only application mail reconciliation. Never mutates mail."""
import re
from datetime import datetime, timezone

from sqlalchemy.orm import joinedload

from app.models.application import Application, ApplicationStatus
from app.services import application_history as history
from app.services.evidence import fingerprint, normal

_EVENTS = (
    ("rejected", r"not (?:be )?(?:moving|proceeding) forward|unfortunately.{0,80}(?:application|position)|other candidates|unable to offer"),
    ("offered", r"(?:pleased|delighted).{0,50}offer|offer of employment|employment offer"),
    ("interview_invited", r"(?:schedule|invite|invitation|availability).{0,80}interview|interview.{0,80}(?:schedule|invite|availability)"),
    ("assessment", r"(?:complete|invitation|invited|assessment link).{0,80}(?:assessment|coding challenge)|assessment.{0,60}(?:deadline|complete)"),
    ("receipt", r"application (?:has been |was )?received|thank you for applying|thanks for applying"),
)


def propose(db, message, received_at=None):
    from app.services.mailbox import _body_text, _decode
    from app.config import live
    if not live().APPLICATION_MAIL_REVIEW:
        return 0
    subject = _decode(message.get("Subject"))[:500]
    if re.search(r"out.of.office|automatic reply|auto.reply|vacation", subject, re.I):
        return 0
    body = _body_text(message)[:16000]
    text = subject + "\n" + body
    kind = next((kind for kind, pattern in _EVENTS if re.search(pattern, text, re.I | re.S)), None)
    if kind is None:
        return 0
    message_id = _decode(message.get("Message-ID"))[:500]
    identity = fingerprint(message_id or [str(message.get("From")), subject, body])
    # Only applications in the search workflow; a job merely discovered by a
    # crawler is not enough evidence that this email concerns the user.
    applications = db.query(Application).options(joinedload(Application.job)).filter(
        Application.status != ApplicationStatus.not_applied).order_by(Application.applied_at.desc()).limit(500).all()
    exact, related = [], []
    text_normal = normal(text)
    for application in applications:
        job = application.job
        urls = [job.url, job.apply_url] + list(job.source_urls or [])
        if any(url and len(url) > 15 and url in text for url in urls):
            exact.append(application)
        elif (len(normal(job.company)) >= 3 and normal(job.company) in text_normal
              and normal(job.title) in text_normal):
            related.append(application)
    candidates = exact or related
    saved = 0
    for application in candidates[:5]:
        match = next((m for _, pattern in _EVENTS if (m := re.search(pattern, text, re.I | re.S))), None)
        snippet = text[max(0, match.start() - 120):match.end() + 240] if match else subject
        event = history.append(db, application, "mail_suggestion", origin="mail", when=received_at or datetime.now(timezone.utc),
            key=f"mail:{identity}:{application.id}", payload={"milestone": kind,
                "message_id": message_id, "subject": subject, "snippet": snippet[:800],
                "association": "posting URL" if exact else "company and role", "ambiguous": len(candidates) > 1,
                "candidate_count": len(candidates), "confirmed": False})
        saved += bool(event)
    return saved
