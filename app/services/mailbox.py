"""
Watching the mailbox, so the tracker stops depending on you remembering.

`OUTREACH_AUTO_DRAFT_FOLLOWUPS` is on by default and the guard against chasing
someone who already answered was `mark_replied` — reachable only by clicking a
button. Nothing observed the mailbox, so the guard against what the outreach
code itself calls "the worst outcome the feature can produce" was a habit rather
than a mechanism.

Two things are read here, and they are read very differently.

**Replies are matched on headers.** A sent message now stores its Message-ID,
and a reply quotes that in `In-Reply-To` or `References`. That is exact: no
guessing from sender and subject, so no marking a recruiter's unrelated mail as
an answer to a specific message. There is a fallback on sender address for mail
sent by hand outside the app — narrower, and hedged accordingly.

**Bounces are the only source of truth about an address.** Contact discovery
guesses addresses from a company's pattern and stores them as `guessed`, and
nothing ever confirmed or refuted one. A delivery failure is that evidence, so
it is what finally sets `email_status="invalid"`.

Read-only, deliberately
-----------------------
The connection selects the folder in readonly mode. This is the user's actual
mailbox; marking their mail as read, or moving it, in order to notice a reply
would be an unreasonable side effect of a tracking feature. Nothing here writes
to the mailbox at all.
"""

import email
import imaplib
import logging
import re
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parseaddr

from app.config import settings
from app.models.outreach import Contact, OutreachMessage
from app.models.profile import Profile

logger = logging.getLogger(__name__)

# Senders that mean "this did not arrive" rather than "somebody wrote to you".
_BOUNCE_SENDERS = re.compile(
    r"(mailer-daemon|postmaster|mail-?delivery|no-?reply.*delivery)", re.I
)
# The recipient a DSN is complaining about.
_FAILED_RECIPIENT_RE = re.compile(
    r"(?:Final-Recipient|Original-Recipient)\s*:\s*[^;]*;\s*<?([^\s>]+@[^\s>]+)", re.I
)
_ANGLE_ADDR_RE = re.compile(r"<([^>]+@[^>]+)>")
# Message-IDs as they appear in In-Reply-To / References.
_MESSAGE_ID_RE = re.compile(r"<[^<>@\s]+@[^<>@\s]+>")

# Headers that mark a machine-generated reply. An out-of-office is not an
# answer, and treating it as one would stop a sequence that should continue.
_AUTO_HEADERS = ("auto-submitted", "x-autoreply", "x-autorespond")


class MailboxError(Exception):
    """A poll that could not run, phrased for whoever has to fix the config."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _setting(name: str, fallback: str = "") -> str:
    return (getattr(settings, name, "") or fallback or "").strip()


def imap_host() -> str:
    return _setting("IMAP_HOST")


def imap_username() -> str:
    # Falls back to the SMTP identity: reading and sending are the same mailbox
    # in every setup this is built for, and asking for it twice invites a typo
    # in one of them.
    return _setting("IMAP_USERNAME", getattr(settings, "SMTP_USERNAME", ""))


def imap_password() -> str:
    return _setting("IMAP_PASSWORD", getattr(settings, "SMTP_PASSWORD", ""))


def mailbox_configured() -> bool:
    return bool(getattr(settings, "IMAP_ENABLED", False) and imap_host() and imap_username())


def mailbox_blocked_reason() -> str:
    """Why polling is not running, or "" when it is."""
    if not getattr(settings, "IMAP_ENABLED", False):
        return "Mailbox polling is off (set IMAP_ENABLED=true to turn it on)."
    if not imap_host():
        return "No IMAP server is configured (set IMAP_HOST, e.g. imap.gmail.com)."
    if not imap_username():
        return "No mailbox username (set IMAP_USERNAME, or SMTP_USERNAME)."
    if not imap_password():
        return "No mailbox password (set IMAP_PASSWORD, or SMTP_PASSWORD)."
    return ""


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def _decode(raw) -> str:
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return str(raw)


def _referenced_ids(message) -> list[str]:
    """Every Message-ID this mail quotes, newest reference first."""
    found: list[str] = []
    for header in ("In-Reply-To", "References"):
        value = message.get(header) or ""
        for match in _MESSAGE_ID_RE.findall(value):
            if match not in found:
                found.append(match)
    return found


def _is_auto_reply(message) -> bool:
    for header in _AUTO_HEADERS:
        value = (message.get(header) or "").lower()
        if value and value != "no":
            return True
    return False


def _body_text(message) -> str:
    """Enough of the body to find a failed recipient in, and no more."""
    chunks: list[str] = []
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        try:
            payload = part.get_payload(decode=True)
        except Exception:
            continue
        if not payload:
            continue
        chunks.append(payload.decode("utf-8", errors="replace"))
        if sum(len(c) for c in chunks) > 100000:
            break
    return "\n".join(chunks)


def _looks_like_bounce(message, sender: str) -> bool:
    if _BOUNCE_SENDERS.search(sender or ""):
        return True
    content_type = (message.get("Content-Type") or "").lower()
    return "report-type=delivery-status" in content_type


def _bounced_address(message) -> str:
    body = _body_text(message)
    match = _FAILED_RECIPIENT_RE.search(body)
    if match:
        return match.group(1).strip().lower()
    # Some providers do not send a conforming DSN. The original recipient is
    # usually still in the text, in angle brackets.
    for candidate in _ANGLE_ADDR_RE.findall(body):
        address = candidate.strip().lower()
        if not _BOUNCE_SENDERS.search(address):
            return address
    return ""


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def _message_by_reference(db, references: list[str]) -> OutreachMessage | None:
    """The sent message a reply is quoting. Exact, or nothing."""
    if not references:
        return None
    return (
        db.query(OutreachMessage)
        .filter(
            OutreachMessage.message_id.in_(references),
            OutreachMessage.status == "sent",
        )
        .first()
    )


def _message_by_sender(db, sender: str, received_at: datetime | None) -> OutreachMessage | None:
    """
    The sent message this address is most likely answering.

    For mail sent by hand — copied into Gmail rather than sent through the app —
    there is no stored Message-ID to match, so the sender address is all there
    is. Kept deliberately tight: the address must be one of our contacts, that
    contact must have a message we recorded as sent, and the reply must not
    predate it. A sequence stopped in error is recoverable; one that keeps
    chasing someone who answered is the outcome this exists to prevent.
    """
    if not sender:
        return None
    contact = db.query(Contact).filter(Contact.email.ilike(sender)).first()
    if not contact:
        return None

    query = (
        db.query(OutreachMessage)
        .filter(
            OutreachMessage.contact_id == contact.id,
            OutreachMessage.status == "sent",
        )
        .order_by(OutreachMessage.sent_at.desc())
    )
    for message in query.all():
        if received_at and message.sent_at and message.sent_at > received_at:
            continue
        return message
    return None


# ---------------------------------------------------------------------------
# Acting
# ---------------------------------------------------------------------------

def _record_reply(db, message: OutreachMessage, when: datetime | None) -> None:
    from app.services.outreach import mark_replied

    mark_replied(db, message, when=when)
    logger.info(
        "mailbox: %s replied — follow-ups for that contact dropped",
        message.contact.email if message.contact else message.id,
    )


def _record_bounce(db, address: str) -> int:
    """
    Mark a dead address dead, on evidence.

    Guessed addresses were stored as plausible and never confirmed. A delivery
    failure is the first hard fact about one, and it stops both the retry and
    the pattern that produced it from looking equally plausible next time.
    """
    contacts = db.query(Contact).filter(Contact.email.ilike(address)).all()
    if not contacts:
        return 0

    affected = 0
    for contact in contacts:
        contact.email_status = "invalid"
        for message in list(contact.messages or []):
            if message.status == "sent":
                message.status = "bounced"
                message.follow_up_due_at = None
                affected += 1
    db.commit()
    logger.info("mailbox: %s bounced — address marked invalid", address)
    return affected


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------

def _state(profile: Profile) -> dict:
    return dict((profile.data or {}).get("mailbox") or {})


def _save_state(db, profile: Profile, state: dict) -> None:
    data = dict(profile.data or {})
    data["mailbox"] = state
    profile.data = data
    db.commit()


def _connect():
    host = imap_host()
    port = int(getattr(settings, "IMAP_PORT", 993))
    try:
        client = imaplib.IMAP4_SSL(host, port, timeout=int(getattr(settings, "IMAP_TIMEOUT", 30)))
    except Exception as exc:
        raise MailboxError(f"Could not reach the mail server: {exc}") from exc
    try:
        client.login(imap_username(), imap_password())
    except imaplib.IMAP4.error as exc:
        raise MailboxError(f"The mail server rejected the login: {exc}") from exc
    return client


def poll(db, limit: int | None = None) -> dict:
    """
    Read what has arrived since last time and act on it.

    Returns counts rather than raising for an empty or uneventful mailbox: no
    new mail is the normal case, and the scheduler should not treat it as a
    problem worth logging.
    """
    counts = {"scanned": 0, "replies": 0, "bounces": 0, "skipped": 0}
    blocked = mailbox_blocked_reason()
    if blocked:
        raise MailboxError(blocked)

    profile = db.query(Profile).first()
    if profile is None:
        raise MailboxError("No profile, so there is nothing to attribute mail to.")

    state = _state(profile)
    folder = getattr(settings, "IMAP_FOLDER", "INBOX") or "INBOX"
    budget = limit or int(getattr(settings, "IMAP_MAX_MESSAGES_PER_POLL", 200))

    client = _connect()
    try:
        # readonly: this is the user's mailbox, and noticing a reply is not a
        # reason to mark their mail as read.
        status, _ = client.select(folder, readonly=True)
        if status != "OK":
            raise MailboxError(f"Could not open the folder {folder!r}.")

        # UIDs are only comparable within one UIDVALIDITY. When it changes the
        # server has renumbered, and a remembered UID means nothing.
        validity = ""
        typ, data = client.status(folder, "(UIDVALIDITY)")
        if typ == "OK" and data:
            match = re.search(rb"UIDVALIDITY (\d+)", data[0] or b"")
            if match:
                validity = match.group(1).decode()
        if validity and state.get("uidvalidity") != validity:
            state = {"uidvalidity": validity}

        last_uid = int(state.get("last_uid") or 0)
        if last_uid:
            criteria = f"(UID {last_uid + 1}:*)"
        else:
            # First run: look back a bounded window rather than the whole
            # mailbox, which on a personal Gmail is years of unrelated mail.
            days = int(getattr(settings, "IMAP_LOOKBACK_DAYS", 14))
            since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%d-%b-%Y")
            criteria = f"(SINCE {since})"

        typ, data = client.uid("SEARCH", None, criteria)
        if typ != "OK":
            raise MailboxError("The mail server refused the search.")
        uids = [u for u in (data[0] or b"").split() if u]
        if len(uids) > budget:
            uids = uids[-budget:]

        highest = last_uid
        for uid in uids:
            typ, fetched = client.uid("FETCH", uid, "(RFC822)")
            if typ != "OK" or not fetched or not fetched[0]:
                continue
            try:
                highest = max(highest, int(uid))
            except ValueError:
                pass

            raw = fetched[0][1]
            if not raw:
                continue
            counts["scanned"] += 1
            try:
                _process(db, email.message_from_bytes(raw), counts)
            except Exception as exc:
                # One malformed mail must not stop the poll, or the whole
                # mailbox stalls behind it forever.
                logger.warning("mailbox: could not process a message: %s", exc)
                counts["skipped"] += 1

        if highest:
            state["last_uid"] = highest
        if validity:
            state["uidvalidity"] = validity
        state["last_poll"] = datetime.now(timezone.utc).isoformat()
        state["last_counts"] = counts
        _save_state(db, profile, state)
    finally:
        try:
            client.logout()
        except Exception:
            pass

    if counts["replies"] or counts["bounces"]:
        logger.info(
            "mailbox: %d replies, %d bounces out of %d scanned",
            counts["replies"], counts["bounces"], counts["scanned"],
        )
    return counts


def _process(db, message, counts: dict) -> None:
    sender = (parseaddr(_decode(message.get("From")))[1] or "").lower()

    if _looks_like_bounce(message, sender):
        address = _bounced_address(message)
        if address and _record_bounce(db, address):
            counts["bounces"] += 1
        return

    if _is_auto_reply(message):
        # An out-of-office is not an answer. Counting it as one would end a
        # sequence that should carry on after they are back.
        counts["skipped"] += 1
        return

    received_at = None
    date_header = message.get("Date")
    if date_header:
        try:
            received_at = email.utils.parsedate_to_datetime(date_header)
            if received_at and received_at.tzinfo is None:
                received_at = received_at.replace(tzinfo=timezone.utc)
        except Exception:
            received_at = None

    target = _message_by_reference(db, _referenced_ids(message))
    if target is None:
        target = _message_by_sender(db, sender, received_at)
    if target is None:
        return

    _record_reply(db, target, received_at)
    counts["replies"] += 1
