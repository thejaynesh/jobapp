"""
Actually delivering an outreach email.

Sending is the one irreversible thing this application does, so it is fenced in
on every side:

  - OUTREACH_SEND_ENABLED is False by default, and no SMTP host means no send
  - only the `email` channel; LinkedIn drafts are copied out by hand
  - only a draft or approved message, never a re-send of one already sent
  - a guessed address is refused unless the caller explicitly overrides
  - a daily cap, counted from the database rather than from memory

Everything raises SendError with a sentence the UI can show, rather than
leaking an smtplib traceback into a toast.
"""

import logging
import mimetypes
import os
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from app.config import settings
from app.models.outreach import OutreachMessage
from app.services.outreach import candidate_email, candidate_name, mark_sent

logger = logging.getLogger(__name__)


class SendError(Exception):
    """A send that could not proceed, phrased for the person who clicked send."""


def sending_configured() -> bool:
    return bool(settings.OUTREACH_SEND_ENABLED and settings.SMTP_HOST)


def sending_blocked_reason() -> str:
    """Why the send button is disabled, or "" when it isn't."""
    if not settings.OUTREACH_SEND_ENABLED:
        return "Email sending is turned off (set OUTREACH_SEND_ENABLED=true to enable it)."
    if not settings.SMTP_HOST:
        return "No SMTP server is configured (set SMTP_HOST and friends)."
    return ""


def sends_today(db) -> int:
    """Messages actually delivered in the last 24 hours, for the daily cap."""
    since = datetime.now(timezone.utc) - timedelta(days=1)
    return (
        db.query(OutreachMessage)
        .filter(
            OutreachMessage.sent_at.isnot(None),
            OutreachMessage.sent_at >= since,
            OutreachMessage.channel == "email",
        )
        .count()
    )


def _from_address(profile_data: dict) -> tuple[str, str]:
    email = settings.SMTP_FROM_EMAIL or candidate_email(profile_data) or settings.SMTP_USERNAME
    name = settings.SMTP_FROM_NAME or candidate_name(profile_data)
    return name, email


def _attachments(db, application) -> list[str]:
    """The current resume and cover letter PDFs, when they exist on disk."""
    if not application or not settings.OUTREACH_ATTACH_DOCUMENTS:
        return []
    from app.models.application import ApplicationDocument

    docs = (
        db.query(ApplicationDocument)
        .filter(
            ApplicationDocument.application_id == application.id,
            ApplicationDocument.is_current.is_(True),
        )
        .all()
    )
    return [d.path for d in docs if d.path and os.path.exists(d.path)]


def build_email(message: OutreachMessage, profile_data: dict, attachments: list[str] | None = None) -> EmailMessage:
    """The MIME message, separated from delivery so it can be inspected in tests."""
    contact = message.contact
    from_name, from_email = _from_address(profile_data)
    if not from_email:
        raise SendError(
            "No sender address. Set SMTP_FROM_EMAIL, or add an email to your profile."
        )

    mail = EmailMessage()
    mail["From"] = formataddr((from_name, from_email)) if from_name else from_email
    mail["To"] = (
        formataddr((contact.name, contact.email)) if contact.name else contact.email
    )
    mail["Subject"] = message.subject or f"Regarding the role at {contact.company}"
    mail["Message-ID"] = make_msgid()
    if from_email:
        mail["Reply-To"] = from_email
    mail.set_content(message.body or "")

    for path in attachments or []:
        guessed, _ = mimetypes.guess_type(path)
        maintype, _, subtype = (guessed or "application/octet-stream").partition("/")
        try:
            with open(path, "rb") as handle:
                mail.add_attachment(
                    handle.read(), maintype=maintype, subtype=subtype,
                    filename=os.path.basename(path),
                )
        except OSError as exc:
            # A missing attachment is not worth failing the send over.
            logger.warning("outreach_sender: could not attach %s: %s", path, exc)
    return mail


def _deliver(mail: EmailMessage) -> None:
    """Hand the message to the SMTP server, translating failures into SendError."""
    try:
        if settings.SMTP_USE_SSL:
            server = smtplib.SMTP_SSL(
                settings.SMTP_HOST, settings.SMTP_PORT, timeout=settings.SMTP_TIMEOUT
            )
        else:
            server = smtplib.SMTP(
                settings.SMTP_HOST, settings.SMTP_PORT, timeout=settings.SMTP_TIMEOUT
            )
        with server:
            server.ehlo()
            if settings.SMTP_USE_TLS and not settings.SMTP_USE_SSL:
                server.starttls()
                server.ehlo()
            if settings.SMTP_USERNAME:
                server.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
            server.send_message(mail)
    except smtplib.SMTPAuthenticationError as exc:
        raise SendError(f"The mail server rejected the login: {exc}") from exc
    except smtplib.SMTPRecipientsRefused as exc:
        raise SendError(
            "The mail server refused the recipient address — it probably doesn't exist."
        ) from exc
    except smtplib.SMTPException as exc:
        raise SendError(f"The mail server refused the message: {exc}") from exc
    except OSError as exc:
        raise SendError(f"Could not reach the mail server: {exc}") from exc


def send_message(db, message: OutreachMessage, allow_guessed: bool = False) -> OutreachMessage:
    """
    Send one drafted email and record the outcome.

    On success the message is marked sent, which is also what schedules its
    follow-up. On failure the message stays a draft with `send_error` set, so
    the user can fix the address and try again.
    """
    blocked = sending_blocked_reason()
    if blocked:
        raise SendError(blocked)
    if message.channel != "email":
        raise SendError(
            f"{message.channel.replace('_', ' ')} messages are sent by hand — "
            "copy the text and mark it sent."
        )
    if message.status in ("sent", "replied"):
        raise SendError("That message has already been sent.")

    contact = message.contact
    if not contact or not contact.email:
        raise SendError("That contact has no email address.")
    if contact.email_status == "invalid":
        raise SendError("That address failed verification — fix it before sending.")
    if contact.email_status == "guessed" and not allow_guessed:
        raise SendError(
            "That address is a pattern guess, not a confirmed one. Send it anyway "
            "only if you accept it may bounce."
        )
    if sends_today(db) >= settings.OUTREACH_MAX_SENDS_PER_DAY:
        raise SendError(
            f"Daily send limit reached ({settings.OUTREACH_MAX_SENDS_PER_DAY}). "
            "Try again tomorrow, or raise OUTREACH_MAX_SENDS_PER_DAY."
        )

    from app.models.profile import Profile

    profile = db.query(Profile).first()
    profile_data = (profile.data if profile else {}) or {}

    mail = build_email(message, profile_data, _attachments(db, message.application))
    try:
        _deliver(mail)
    except SendError as exc:
        message.send_error = str(exc)[:500]
        db.commit()
        raise

    mark_sent(db, message)
    logger.info("outreach_sender: sent message %s to %s", message.id, contact.email)
    return message
