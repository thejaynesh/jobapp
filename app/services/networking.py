"""Durable relationship context shared by discovery, drafts and the inbox."""
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from sqlalchemy import func, or_, text

from app.config import live
from app.models.outreach import (
    Contact, ContactDiscoveryCache, NetworkPerson, OutreachConversation,
    OutreachInteraction, OutreachMessage,
)

_source_issue = ContextVar("outreach_source_issue", default=None)


def now():
    return datetime.now(timezone.utc)


def canonical_profile(url: str | None) -> str | None:
    parsed = urlparse((url or "").strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"https", "http"} or not (host == "linkedin.com" or host.endswith(".linkedin.com")):
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) != 2 or parts[0].lower() != "in":
        return None
    return f"https://www.linkedin.com/in/{parts[1].lower()}"


def ensure_person(db, contact: Contact) -> NetworkPerson:
    if contact.person is not None:
        return contact.person
    email = (contact.email or "").strip().lower() or None
    profile = canonical_profile(contact.linkedin_url)
    # Discovery and manual additions may race on the same exact identity.
    db.execute(text("SELECT pg_advisory_xact_lock(2006934002)"))
    person = db.query(NetworkPerson).filter(NetworkPerson.primary_email == email).first() if email else None
    profile_person = db.query(NetworkPerson).filter(NetworkPerson.linkedin_url == profile).first() if profile else None
    if person is None and profile_person is not None:
        # A profile is an identity; a newly learned address can be an alias.
        person = profile_person
    if person is None and email:
        prior = db.query(Contact).filter(func.lower(Contact.email) == email, Contact.person_id.isnot(None)).first()
        person = prior.person if prior else None
    if person is None:
        person = NetworkPerson(name=contact.name, primary_email=email, linkedin_url=profile,
                               relationship_kind="unknown", do_not_contact=False)
        db.add(person)
        db.flush()
    elif profile_person is not None and profile_person.id != person.id:
        contact.evidence = {**(contact.evidence or {}), "identity_conflict": "Email and profile identify different saved people; review the profile."}
    if not person.name and contact.name:
        person.name = contact.name
    if not person.linkedin_url and profile and profile_person is None:
        person.linkedin_url = profile
    contact.person = person
    contact.person_id = person.id
    return person


def conversation_key(company: str) -> str:
    """Preserve substantive employer names; similarity is not identity evidence."""
    return " ".join((company or "").casefold().split())


def conversation_for(db, contact: Contact) -> OutreachConversation:
    person = ensure_person(db, contact)
    key = conversation_key(contact.company)
    conversation = db.query(OutreachConversation).filter_by(person_id=person.id, company_key=key).first()
    if conversation is None:
        db.execute(text("SELECT pg_advisory_xact_lock(2006934002)"))
        conversation = db.query(OutreachConversation).filter_by(person_id=person.id, company_key=key).first()
        if conversation is None:
            conversation = OutreachConversation(person=person, company_key=key,
                company=contact.company, application_id=contact.application_id,
                status="researching", last_activity_at=now())
            db.add(conversation)
            db.flush()
    # Lazily cover contacts created by extensions and older callers.
    for message in contact.messages or []:
        if message.conversation_id is None:
            message.conversation = conversation
    return conversation


def same_person_contacts(db, contact: Contact) -> list[Contact]:
    clauses = [Contact.id == contact.id]
    if contact.person_id:
        clauses.append(Contact.person_id == contact.person_id)
    if contact.email:
        clauses.append(func.lower(Contact.email) == contact.email.lower())
    profile = canonical_profile(contact.linkedin_url)
    if profile:
        clauses.append(Contact.linkedin_url == profile)
    return db.query(Contact).filter(or_(*clauses)).all()


def relationship_context(db, contact: Contact) -> str:
    conversation = conversation_for(db, contact)
    person = conversation.person
    lines = [f"Relationship: {person.relationship_kind}. {person.relationship_notes or ''}",
             f"Contact notes supplied by the user: {contact.notes or ''}",
             f"Conversation notes: {conversation.notes or ''}"]
    contact_ids = [c.id for c in same_person_contacts(db, contact)]
    messages = db.query(OutreachMessage).filter(OutreachMessage.contact_id.in_(contact_ids),
        OutreachMessage.status.in_(("sent", "replied"))).order_by(OutreachMessage.sent_at.desc()).limit(4).all()
    interactions = db.query(OutreachInteraction).join(OutreachConversation).filter(
        OutreachConversation.person_id == person.id).order_by(OutreachInteraction.occurred_at.desc()).limit(4).all()
    events = [(m.sent_at or m.created_at, f"You wrote ({m.contact.company}): {(m.body or '')[:700]}") for m in messages]
    events += [(i.occurred_at, f"{i.kind}: {i.body[:1000]}") for i in interactions]
    for when, detail in sorted(events, key=lambda event: event[0])[-6:]:
        lines.append(f"[{when:%Y-%m-%d}] {detail}")
    return "\n\n".join(lines)


def record_interaction(db, conversation, kind: str, body: str = "", message_id: str | None = None, when=None):
    if message_id and db.query(OutreachInteraction).filter_by(message_id=message_id).first():
        return None
    interaction = OutreachInteraction(conversation=conversation, kind=kind, body=(body or "")[:8000],
                                     message_id=message_id, occurred_at=when or now())
    db.add(interaction)
    conversation.last_activity_at = when or now()
    return interaction


def record_address_bounce(db, address: str) -> int:
    """Apply the same delivery evidence to every record of this exact address."""
    contacts = db.query(Contact).filter(func.lower(Contact.email) == address.strip().lower()).all()
    affected, conversations = 0, set()
    stamp = now()
    for contact in contacts:
        was_invalid = contact.email_status == "invalid"
        contact.email_status = "invalid"
        contact.evidence = {**(contact.evidence or {}), "bounce_recorded_at": stamp.isoformat()}
        for message in contact.messages:
            message.follow_up_due_at = None
            if message.status == "sent":
                message.status = "bounced"
                affected += 1
            elif (message.kind == "follow_up" and message.status in {"draft", "approved"}
                    and message.send_state not in {"sending", "uncertain"} and not message.delivery_uncertain):
                message.status = "skipped"
        conversation = conversation_for(db, contact)
        if conversation.id in conversations:
            continue
        conversations.add(conversation.id)
        if not was_invalid:
            record_interaction(db, conversation, "bounce", f"Delivery to {address} failed.", when=stamp)
        if conversation.status in {"awaiting_reply", "ready"}:
            conversation.status = "researching"
        if not conversation.next_action or conversation.next_action in {
                "Await reply", "Review draft", "Review bounced address", "Follow-up draft failed; review and retry"}:
            conversation.next_action = "Review bounced address"
            conversation.next_action_due_at = stamp
    return affected


def pause_reason(db, contact: Contact, application=None, kind=None) -> str:
    conversation = conversation_for(db, contact)
    if conversation.person.do_not_contact:
        return "This person is marked do not contact. Update the relationship before sending."
    if conversation.status == "closed":
        return "This conversation is closed. Reopen it before sending."
    until = conversation.snoozed_until
    if until and until > now():
        return f"This conversation is snoozed until {until:%Y-%m-%d}."
    status = getattr(getattr(application, "status", None), "value", None)
    if status in {"rejected", "withdrawn"} and kind not in {"reply", "thank_you"}:
        return "This application is closed. Start a separate reconnect conversation for relationship follow-up."
    return ""


def source_issue(message: str):
    issues = _source_issue.get()
    if issues is not None:
        issues.append(message[:500])


def cached_discovery(db, company_key: str, domain: str, source: str, fetch, scope: str = ""):
    """Cache only completed source reads; failures remain visible and retryable."""
    import hashlib
    key = hashlib.sha256(f"{company_key}|{domain}|{source}|{scope}".encode()).hexdigest()
    row = db.get(ContactDiscoveryCache, key)
    stamp = now()
    if row and row.status in {"ok", "empty"} and row.expires_at > stamp:
        return row.data.get("result", []), {"source": source, "status": row.status, "cached": True, "checked_at": row.checked_at.isoformat()}
    token = _source_issue.set([])
    try:
        result = fetch()
        error = "; ".join(dict.fromkeys(_source_issue.get() or []))[:500] or None
    except Exception as exc:
        result, error = [], str(exc)[:500]
    finally:
        _source_issue.reset(token)
    if isinstance(result, dict):
        empty = not result.get("emails") and not result.get("email")
    else:
        empty = not result
    status = "failed" if error else ("empty" if empty else "ok")
    hours = live().OUTREACH_DISCOVERY_CACHE_HOURS if status == "ok" else live().OUTREACH_DISCOVERY_EMPTY_CACHE_HOURS
    if row is None:
        row = ContactDiscoveryCache(key=key, company_key=company_key, source=source)
        db.add(row)
    row.status, row.error, row.checked_at = status, error, stamp
    row.expires_at = stamp if error else stamp + timedelta(hours=hours)
    row.data = {"result": result}
    db.flush()
    return result, {"source": source, "status": status, "error": error, "cached": False, "checked_at": stamp.isoformat()}
