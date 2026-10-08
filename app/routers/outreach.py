import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from app.templating import build as build_templates
from sqlalchemy.orm import Session
from sqlalchemy import or_, func

from app.config import live, settings
from app.database import get_db
from app.models.application import Application
from app.models.outreach import (
    CONTACT_ROLES, Contact, MESSAGE_CHANNELS, MESSAGE_KINDS, MESSAGE_STATUSES,
    OutreachMessage, OutreachConversation, NetworkPerson, CONVERSATION_STATUSES, RELATIONSHIP_KINDS,
)
from app.services.outreach import (
    TONES, draft_message, outreach_stats, regenerate_message, run_outreach,
    set_message_status,
)

logger = logging.getLogger(__name__)
from app.services import networking

# One router, no prefix: the page and its HTMX fragments live under /outreach,
# while the JSON trigger stays where the API had it.
router = APIRouter(tags=["outreach"])
templates = build_templates()


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------

def _get_application(db: Session, app_id: uuid.UUID) -> Application:
    app_obj = db.query(Application).filter(Application.id == app_id).first()
    if not app_obj:
        raise HTTPException(status_code=404, detail="Application not found")
    return app_obj


def _get_contact(db: Session, contact_id: uuid.UUID) -> Contact:
    contact = db.query(Contact).filter(Contact.id == contact_id).first()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    return contact


def _get_message(db: Session, message_id: uuid.UUID) -> OutreachMessage:
    message = db.query(OutreachMessage).filter(OutreachMessage.id == message_id).first()
    if not message:
        raise HTTPException(status_code=404, detail="Message not found")
    return message


def _panel(request: Request, db: Session, app_obj: Application, notice: dict | None = None):
    """The whole outreach panel on an application, re-rendered after every action."""
    return templates.TemplateResponse(
        "outreach/partials/panel.html",
        {"request": request, **panel_context(db, app_obj), "notice": notice},
    )


def panel_context(db: Session, app_obj: Application) -> dict:
    """
    Everything the outreach panel renders.

    Shared with the application detail page, which embeds the same partial —
    two copies of this dict would drift the moment either gained a field.
    """
    from app.services.outreach import (
        contact_message_link, discovery_stale, prior_conversations, search_links,
    )
    from app.services.outreach_sender import sending_blocked_reason

    contacts = [c for c in app_obj.contacts if not c.archived]
    conversations = {c.id: networking.conversation_for(db, c) for c in contacts}
    db.commit()
    return {
        "app": app_obj,
        "contacts": contacts,
        "conversations": conversations,
        "conversation_statuses": CONVERSATION_STATUSES,
        "relationship_kinds": RELATIONSHIP_KINDS,
        "archived_count": sum(1 for c in app_obj.contacts if c.archived),
        # Pre-built LinkedIn searches — the path that works with no API key and
        # no risk to the user's account.
        "search_links": search_links(db, app_obj),
        "contact_links": {c.id: contact_message_link(c) for c in contacts},
        # A "discovering" status older than the task's own time limit means the
        # worker never came back; the panel must stop polling and re-offer the button.
        "discovery_stale": discovery_stale(app_obj),
        # Warns before writing to someone already approached about another role.
        "prior": {c.id: prior_conversations(db, c) for c in contacts},
        "channels": MESSAGE_CHANNELS,
        "kinds": MESSAGE_KINDS,
        "tones": list(TONES),
        "roles": CONTACT_ROLES,
        "send_blocked": sending_blocked_reason(),
        "notice": None,
    }


def _panel_for_contact(request: Request, db: Session, contact: Contact, notice: dict | None = None):
    """
    Re-render the panel a contact belongs to.

    A contact can outlive its application, in which case there is no panel to
    swap and the caller gets a one-line confirmation instead.
    """
    if contact.application is None or "/outreach/contacts/" in request.headers.get("HX-Current-URL", ""):
        return _contact_panel(request, db, contact, notice)
    db.refresh(contact.application)
    return _panel(request, db, contact.application, notice)


def _contact_context(db, contact):
    from app.services.outreach import contact_message_link, prior_conversations
    from app.services.outreach_sender import sending_blocked_reason
    conversation = networking.conversation_for(db, contact)
    db.commit()
    return dict(app=None, contacts=[contact], archived_count=0, conversations={contact.id: conversation},
        conversation_statuses=CONVERSATION_STATUSES, relationship_kinds=RELATIONSHIP_KINDS,
        contact_links={contact.id: contact_message_link(contact)}, prior={contact.id: prior_conversations(db, contact)},
        search_links=[], discovery_stale=False, channels=MESSAGE_CHANNELS, kinds=MESSAGE_KINDS,
        tones=list(TONES), roles=CONTACT_ROLES, send_blocked=sending_blocked_reason())


def _contact_panel(request, db, contact, notice=None):
    return templates.TemplateResponse("outreach/partials/panel.html",
        {"request": request, **_contact_context(db, contact), "notice": notice})


# ---------------------------------------------------------------------------
# The outreach page
# ---------------------------------------------------------------------------

@router.get("/outreach", response_class=HTMLResponse)
def outreach_home(request: Request, q: str = "", channel: str = "", page: int = 1, state: str = "",
                  db: Session = Depends(get_db)):
    """
    Everything in flight, in the order it needs attention.

    Drafts waiting for a decision come first, then what has been sent and is
    still unanswered, then replies. The point is to answer "what do I do now"
    without opening every application.
    """
    from app.services.outreach import due_follow_ups
    from app.services.outreach_sender import sending_blocked_reason

    page = max(1, page)
    query = db.query(OutreachMessage).join(Contact, Contact.id == OutreachMessage.contact_id)
    if q:
        query = query.filter(or_(Contact.name.icontains(q, autoescape=True), Contact.email.icontains(q, autoescape=True),
            Contact.company.icontains(q, autoescape=True), OutreachMessage.body.icontains(q, autoescape=True),
            OutreachMessage.subject.icontains(q, autoescape=True)))
    if channel:
        query = query.filter(OutreachMessage.channel == channel)
    if state in MESSAGE_STATUSES:
        query = query.filter(OutreachMessage.status == state)
    total = query.count()
    messages = query.order_by(OutreachMessage.created_at.desc(), OutreachMessage.id).offset((page - 1) * 50).limit(50).all()

    by_status: dict[str, list] = {status: [] for status in MESSAGE_STATUSES}
    for message in messages:
        by_status.setdefault(message.status, []).append(message)

    contact_query = (
        db.query(Contact)
        .filter(Contact.archived.is_(False), ~Contact.messages.any())
    )
    if q:
        contact_query = contact_query.filter(or_(Contact.name.icontains(q, autoescape=True), Contact.company.icontains(q, autoescape=True)))
    contactless = contact_query.order_by(Contact.created_at.desc()).offset((page - 1) * 50).limit(50).all()
    actions_query = db.query(OutreachConversation).join(NetworkPerson).filter(
        OutreachConversation.status != "closed", NetworkPerson.do_not_contact.is_(False),
        or_(OutreachConversation.snoozed_until.is_(None), OutreachConversation.snoozed_until <= networking.now()))
    if q:
        actions_query = actions_query.filter(or_(NetworkPerson.name.icontains(q, autoescape=True), OutreachConversation.company.icontains(q, autoescape=True)))
    actions = actions_query.order_by(OutreachConversation.next_action_due_at.asc().nullslast(), OutreachConversation.last_activity_at.desc()).limit(50).all()

    return templates.TemplateResponse(
        "outreach/index.html",
        {
            "request": request,
            "stats": outreach_stats(db),
            "drafts": by_status["draft"] + by_status["approved"],
            "awaiting": [m for m in by_status["sent"] if not m.replied_at],
            "replied": by_status["replied"],
            "closed": by_status["bounced"] + by_status["skipped"],
            "due": [m for m in messages if m.follow_up_due_at and m.follow_up_due_at <= networking.now() and m.status == "sent"],
            "no_message_contacts": contactless,
            "send_blocked": sending_blocked_reason(),
            "channels": MESSAGE_CHANNELS,
            "kinds": MESSAGE_KINDS,
            "tones": list(TONES),
            "q": q,
            "channel_filter": channel,
            "state_filter": state, "page": page, "total": total, "has_more": page * 50 < total or len(contactless) == 50,
            "actions": actions, "relationship_kinds": RELATIONSHIP_KINDS,
        },
    )


@router.get("/outreach/contacts/{contact_id}", response_class=HTMLResponse)
def contact_workspace(contact_id: uuid.UUID, request: Request, db: Session = Depends(get_db)):
    contact = _get_contact(db, contact_id)
    return templates.TemplateResponse("outreach/contact.html", {"request": request, "contact": contact,
        **_contact_context(db, contact), "notice": None})


@router.get("/outreach/contacts/{contact_id}/panel", response_class=HTMLResponse)
def contact_panel(contact_id: uuid.UUID, request: Request, db: Session = Depends(get_db)):
    return _contact_panel(request, db, _get_contact(db, contact_id))


@router.get("/outreach/conversations/{conversation_id}")
def open_conversation(conversation_id: uuid.UUID, db: Session = Depends(get_db)):
    conversation = db.get(OutreachConversation, conversation_id)
    if not conversation:
        raise HTTPException(404, "Conversation not found")
    candidates = db.query(Contact).filter_by(person_id=conversation.person_id).order_by(Contact.archived, Contact.created_at.desc()).all()
    contact = next((row for row in candidates if networking.conversation_key(row.company) == conversation.company_key), None)
    if not contact:
        raise HTTPException(404, "Contact not found")
    return RedirectResponse(f"/outreach/contacts/{contact.id}", status_code=303)


@router.get("/outreach/apps/{app_id}/panel", response_class=HTMLResponse)
def outreach_panel(app_id: uuid.UUID, request: Request, db: Session = Depends(get_db)):
    """The panel on its own, so the page can poll it while discovery runs."""
    return _panel(request, db, _get_application(db, app_id))


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

@router.post("/outreach/apps/{app_id}/discover", response_class=HTMLResponse)
def discover(
    app_id: uuid.UUID,
    request: Request,
    use_linkedin: bool = Form(False),
    db: Session = Depends(get_db),
):
    """
    Queue contact discovery.

    Queued rather than run inline: Hunter plus a LinkedIn browser launch is a
    minute of work, and the panel polls itself until the worker is done.
    """
    from app.services.outreach import discovery_stale

    app_obj = _get_application(db, app_id)
    if not live().OUTREACH_ENABLED:
        return _panel(request, db, app_obj,
                      {"ok": False, "message": "Outreach is disabled in configuration."})
    if app_obj.outreach_status == "discovering" and not discovery_stale(app_obj):
        return _panel(request, db, app_obj,
                      {"ok": False, "message": "A search is already running."})

    app_obj.outreach_status = "discovering"
    app_obj.outreach_error = None
    # Stamped at the start, not the end: it is what tells the panel whether an
    # in-progress search is still plausible or belongs to a worker that died.
    app_obj.outreach_checked_at = datetime.now(timezone.utc)
    db.commit()

    try:
        from app.tasks.outreach import discover_contacts_task
        discover_contacts_task.delay(str(app_obj.id))
    except Exception as exc:
        logger.error("outreach: could not queue discovery for %s: %s", app_id, exc)
        app_obj.outreach_status = "failed"
        app_obj.outreach_error = f"Could not queue the search: {exc}"
        db.commit()
        return _panel(request, db, app_obj,
                      {"ok": False, "message": app_obj.outreach_error})

    if use_linkedin and not settings.LINKEDIN_SESSION_COOKIE:
        logger.info("outreach: LinkedIn search asked for without a session cookie")

    return _panel(request, db, app_obj,
                  {"ok": True, "message": "Searching for contacts — this panel updates itself."})


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------

@router.post("/outreach/apps/{app_id}/contacts", response_class=HTMLResponse)
def add_contact(
    app_id: uuid.UUID,
    request: Request,
    name: str = Form(""),
    title: str = Form(""),
    email: str = Form(""),
    linkedin_url: str = Form(""),
    role: str = Form("unknown"),
    db: Session = Depends(get_db),
):
    """Add someone by hand — the case discovery can't cover, like a warm intro."""
    from app.services.company_domain import company_key, registrable_domain
    from app.services.contact_finder import split_name

    app_obj = _get_application(db, app_id)
    email = email.strip().lower()
    name = name.strip()
    if not (email or linkedin_url.strip()):
        return _panel(request, db, app_obj,
                      {"ok": False, "message": "A contact needs an email or a LinkedIn URL."})
    if email and "@" not in email:
        return _panel(request, db, app_obj,
                      {"ok": False, "message": f"{email} is not an email address."})

    first, last = split_name(name)
    existing = db.query(Contact).filter(Contact.application_id == app_id, Contact.email == email).first() if email else None
    if existing:
        return _panel(request, db, app_obj, {"ok": True, "message": "This address is already saved for this application."})
    contact = Contact(
        application_id=app_obj.id,
        company=app_obj.job.company,
        company_key=company_key(app_obj.job.company),
        name=name or None,
        first_name=first or None,
        last_name=last or None,
        title=title.strip() or None,
        email=email or None,
        # Typed in by a human who presumably knows it — trusted, but not checked.
        email_status="unverified" if email else "unknown",
        email_confidence=80 if email else 0,
        linkedin_url=linkedin_url.strip() or None,
        role=role if role in CONTACT_ROLES else "unknown",
        source="manual",
        domain=registrable_domain(email.rsplit("@", 1)[-1]) if email else None,
    )
    db.add(contact)
    networking.conversation_for(db, contact)
    db.commit()
    return _panel(request, db, app_obj, {"ok": True, "message": f"Added {contact.display_name}."})


@router.post("/outreach/contacts/{contact_id}/update", response_class=HTMLResponse)
def update_contact(
    contact_id: uuid.UUID,
    request: Request,
    name: str = Form(""),
    title: str = Form(""),
    email: str = Form(""),
    linkedin_url: str = Form(""),
    role: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    """Correct a discovered contact. A corrected address is treated as confirmed."""
    contact = _get_contact(db, contact_id)
    email = email.strip().lower()
    if email and "@" not in email:
        return _panel_for_contact(request, db, contact,
                                  {"ok": False, "message": f"{email} is not an email address."})

    if email != (contact.email or ""):
        contact.email = email or None
        contact.email_status = "unverified" if email else "unknown"
        contact.email_confidence = 80 if email else 0
    contact.name = name.strip() or None
    contact.title = title.strip() or None
    contact.linkedin_url = linkedin_url.strip() or None
    contact.notes = notes.strip() or None
    if role in CONTACT_ROLES:
        contact.role = role
    person = networking.ensure_person(db, contact)
    if contact.name:
        person.name = contact.name
    db.commit()
    return _panel_for_contact(request, db, contact, {"ok": True, "message": "Contact saved."})


@router.post("/outreach/contacts/{contact_id}/archive", response_class=HTMLResponse)
def archive_contact(contact_id: uuid.UUID, request: Request, db: Session = Depends(get_db)):
    """
    Hide a contact and stop chasing them.

    Archiving rather than deleting: the messages already sent to them are part
    of the record, and a rediscovery shouldn't resurrect someone dismissed.
    """
    contact = _get_contact(db, contact_id)
    contact.archived = True
    for message in contact.messages or []:
        message.follow_up_due_at = None
        if message.status in ("draft", "approved"):
            message.status = "skipped"
    db.commit()
    return _panel_for_contact(request, db, contact,
                              {"ok": True, "message": f"Archived {contact.display_name}."})


@router.post("/outreach/contacts/{contact_id}/verify", response_class=HTMLResponse)
def verify_contact_email(contact_id: uuid.UUID, request: Request, db: Session = Depends(get_db)):
    """Spend a Hunter verifier credit to find out whether an address is real."""
    from app.services.contact_finder import verify_email

    contact = _get_contact(db, contact_id)
    if not contact.email:
        return _panel_for_contact(request, db, contact,
                                  {"ok": False, "message": "That contact has no address to check."})
    if not settings.HUNTER_IO_API_KEY:
        return _panel_for_contact(request, db, contact,
                                  {"ok": False, "message": "Verification needs HUNTER_IO_API_KEY."})

    result = verify_email(contact.email, settings.HUNTER_IO_API_KEY)
    if not result:
        return _panel_for_contact(request, db, contact,
                                  {"ok": False, "message": "The verifier didn't answer — try again later."})
    if result.get("pending"):
        contact.evidence = {**(contact.evidence or {}), "verification_pending": True}
        db.commit()
        from app.tasks.outreach import verify_contact_email_task
        try:
            verify_contact_email_task.apply_async(args=[str(contact.id)], countdown=30)
        except Exception:
            contact.evidence = {**contact.evidence, "verification_pending": False, "verification_error": "Could not queue verification; try again."}
            db.commit()
            return _panel_for_contact(request, db, contact, {"ok": False, "message": "Could not queue verification; try again."})
        return _panel_for_contact(request, db, contact, {"ok": True, "message": "Verification is pending. The result will be saved automatically."})
    contact.email_status = result["status"]
    contact.email_confidence = result["confidence"]
    contact.evidence = {**(contact.evidence or {}), "verification_pending": False,
        "verification": result.get("verification") or {}, "verification_checked_at": networking.now().isoformat()}
    db.commit()
    return _panel_for_contact(
        request, db, contact,
        {"ok": result["status"] != "invalid",
         "message": f"{contact.email} is {result['status'].replace('_', ' ')}."},
    )


class ContactCapture(BaseModel):
    company: str
    name: str = ""
    title: str = ""
    email: str = ""
    linkedin_url: str = ""
    profile_url: str = ""
    source_url: str = ""
    notes: str = ""
    application_id: uuid.UUID | None = None
    relationship_kind: str = "unknown"


def _capture_contact(db, data: ContactCapture, source="browser"):
    from urllib.parse import urlparse
    from app.services.company_domain import company_key
    from app.services.contact_finder import split_name, EMAIL_RE
    company = data.company.strip()[:200]
    if not company:
        raise HTTPException(422, "A company is required")
    email = data.email.strip().lower()
    if email and not EMAIL_RE.fullmatch(email):
        raise HTTPException(422, "Enter a valid email address")
    linkedin = networking.canonical_profile(data.linkedin_url)
    if data.linkedin_url and not linkedin:
        raise HTTPException(422, "Enter an individual LinkedIn profile URL")
    for url in (data.profile_url, data.source_url):
        if url and (urlparse(url).scheme not in {"http", "https"} or not urlparse(url).hostname):
            raise HTTPException(422, "Source and profile URLs must use HTTP or HTTPS")
    if not (email or linkedin or data.profile_url):
        raise HTTPException(422, "Provide an email or public profile")
    application = _get_application(db, data.application_id) if data.application_id else None
    if application:
        company = application.job.company
    query = db.query(Contact).filter_by(application_id=data.application_id, company_key=company_key(company))
    existing = query.filter(Contact.email == email).first() if email else query.filter(Contact.linkedin_url == linkedin).first() if linkedin else None
    contact = existing or Contact(application_id=data.application_id, company=company, company_key=company_key(company),
        email=email or None, linkedin_url=linkedin, email_status="unverified" if email else "unknown", source=source, role="unknown")
    if not existing:
        db.add(contact)
    contact.name = data.name.strip()[:200] or contact.name
    contact.title = data.title.strip()[:300] or contact.title
    contact.first_name, contact.last_name = split_name(contact.name or "")
    contact.profile_url = data.profile_url.strip() or contact.profile_url
    contact.linkedin_url = contact.linkedin_url or linkedin
    if data.notes.strip() and data.notes.strip() not in (contact.notes or ""):
        contact.notes = "\n\n".join(filter(None, [contact.notes, data.notes.strip()]))[:5000]
    evidence = dict(contact.evidence or {})
    sources = list(evidence.get("sources") or [])
    source_url = data.source_url or linkedin or data.profile_url
    if source_url and not any(item.get("uri") == source_url for item in sources if isinstance(item, dict)):
        sources.append({"uri": source_url, "kind": source, "recorded_at": networking.now().isoformat()})
    contact.evidence = {**evidence, "sources": sources}
    if application:
        contact.company_id = application.job.company_id
    person = networking.ensure_person(db, contact)
    if data.relationship_kind in RELATIONSHIP_KINDS and data.relationship_kind != "unknown":
        person.relationship_kind = data.relationship_kind
    networking.conversation_for(db, contact)
    db.commit()
    return contact


@router.post("/api/outreach/contacts/capture", status_code=201)
@router.post("/api/agent/outreach-contact", status_code=201)
def capture_contact(data: ContactCapture, db: Session = Depends(get_db)):
    contact = _capture_contact(db, data)
    return {"contact_id": str(contact.id), "url": f"/outreach/contacts/{contact.id}"}


@router.post("/outreach/contacts")
def add_network_contact(company: str = Form(...), name: str = Form(""), title: str = Form(""),
    email: str = Form(""), linkedin_url: str = Form(""), notes: str = Form(""),
    relationship_kind: str = Form("unknown"), db: Session = Depends(get_db)):
    contact = _capture_contact(db, ContactCapture(company=company, name=name, title=title, email=email,
        linkedin_url=linkedin_url, notes=notes, relationship_kind=relationship_kind), source="manual")
    return RedirectResponse(f"/outreach/contacts/{contact.id}", status_code=303)


@router.post("/outreach/contacts/{contact_id}/relationship", response_class=HTMLResponse)
def update_relationship(contact_id: uuid.UUID, request: Request, status: str = Form("researching"),
    relationship_kind: str = Form("unknown"), relationship_notes: str = Form(""),
    next_action: str = Form(""), due: str = Form(""), snooze: str = Form(""),
    notes: str = Form(""), interaction: str = Form(""), do_not_contact: bool = Form(False),
    db: Session = Depends(get_db)):
    contact = _get_contact(db, contact_id)
    if status not in CONVERSATION_STATUSES or relationship_kind not in RELATIONSHIP_KINDS:
        raise HTTPException(422, "Choose a valid conversation and relationship status")
    def date_value(value):
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
            from app.services.timefmt import zone
            return (parsed.replace(tzinfo=zone()) if parsed.tzinfo is None else parsed).astimezone(timezone.utc)
        except ValueError:
            raise HTTPException(422, "Choose a valid date")
    due_at, snoozed_until = date_value(due), date_value(snooze)
    if status == "snoozed" and (snoozed_until is None or snoozed_until <= networking.now()):
        raise HTTPException(422, "Choose a future date to snooze this conversation")
    conversation = networking.conversation_for(db, contact)
    previous = conversation.status
    conversation.status, conversation.notes = status, notes.strip()[:5000] or None
    conversation.next_action = next_action.strip()[:500] or None
    conversation.next_action_due_at = due_at
    conversation.snoozed_until = snoozed_until if status == "snoozed" else None
    if status == "snoozed":
        conversation.next_action_due_at = snoozed_until
    person = conversation.person
    person.relationship_kind, person.relationship_notes = relationship_kind, relationship_notes.strip()[:5000] or None
    person.do_not_contact = do_not_contact
    if interaction.strip() or status != previous:
        networking.record_interaction(db, conversation, status, interaction.strip() or f"Conversation marked {status.replace('_', ' ')} by you.")
    if status in {"closed", "reply_needed", "introduced", "referral_offered", "referral_submitted"} or do_not_contact:
        for linked in networking.same_person_contacts(db, contact):
            for message in linked.messages:
                message.follow_up_due_at = None
                if message.kind == "follow_up" and message.status in {"draft", "approved"}:
                    message.status = "skipped"
    db.commit()
    return _panel_for_contact(request, db, contact, {"ok": True, "message": "Relationship and next action saved."})


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

@router.post("/outreach/contacts/{contact_id}/draft", response_class=HTMLResponse)
def create_draft(
    contact_id: uuid.UUID,
    request: Request,
    channel: str = Form(""),
    kind: str = Form("initial"),
    tone: str = Form("warm"),
    feedback: str = Form(""),
    db: Session = Depends(get_db),
):
    """Write a new message for a contact."""
    contact = _get_contact(db, contact_id)
    try:
        message = draft_message(
            db, contact,
            channel=channel or None,
            kind=kind,
            tone=tone if tone in TONES else "warm",
            feedback=feedback.strip() or None,
        )
    except Exception as exc:
        logger.error("outreach: draft failed for contact %s: %s", contact_id, exc)
        db.rollback()
        return _panel_for_contact(request, db, contact,
                                  {"ok": False, "message": f"Could not write that draft: {exc}"})
    return _panel_for_contact(
        request, db, contact,
        {"ok": True, "message": f"Drafted a {message.kind.replace('_', ' ')} message."},
    )


@router.post("/outreach/messages/{message_id}/regenerate", response_class=HTMLResponse)
def regenerate(
    message_id: uuid.UUID,
    request: Request,
    feedback: str = Form(""),
    db: Session = Depends(get_db),
):
    """Rewrite a draft, optionally with instructions on what to change."""
    message = _get_message(db, message_id)
    try:
        regenerate_message(db, message, feedback.strip() or None)
    except ValueError as exc:
        return _panel_for_contact(request, db, message.contact, {"ok": False, "message": str(exc)})
    except Exception as exc:
        logger.error("outreach: regenerate failed for %s: %s", message_id, exc)
        db.rollback()
        return _panel_for_contact(request, db, message.contact,
                                  {"ok": False, "message": f"Could not rewrite it: {exc}"})
    return _panel_for_contact(request, db, message.contact, {"ok": True, "message": "Rewritten."})


@router.post("/outreach/messages/{message_id}/save", response_class=HTMLResponse)
def save_message(
    message_id: uuid.UUID,
    subject: str = Form(""),
    body: str = Form(""),
    db: Session = Depends(get_db),
):
    """
    Save a hand-edited draft.

    Returns a one-line confirmation rather than the panel: this fires while the
    user is typing, and swapping the panel would move the cursor out of the box.
    """
    message = _get_message(db, message_id)
    # Serialize a delayed autosave with the sender's reservation. Once SMTP
    # starts, the stored body must stay the body handed to the mail server.
    db.refresh(message, with_for_update=True)
    if message.sent_at or message.status in ("sent", "replied", "bounced"):
        raise HTTPException(status_code=409, detail="That message has already been sent.")
    if message.send_state in ("sending", "uncertain") or message.delivery_uncertain:
        raise HTTPException(status_code=409, detail="Delivery has started; this message cannot be edited.")
    message.subject = subject.strip() or None
    message.body = body
    message.edited = True
    db.commit()
    return HTMLResponse('<span class="text-xs text-green-600">Saved</span>')


@router.post("/outreach/messages/{message_id}/status", response_class=HTMLResponse)
def update_message_status(
    message_id: uuid.UUID,
    request: Request,
    status: str = Form(...),
    save_draft: bool = Form(False),
    subject: str = Form(""),
    body: str = Form(""),
    db: Session = Depends(get_db),
):
    """
    Move a message along: approved, sent by hand, replied to, or abandoned.

    Marking it sent here is what starts the follow-up clock, so the sequence
    works the same whether or not SMTP is configured.
    """
    message = _get_message(db, message_id)
    db.refresh(message, with_for_update=True)
    # Manual delivery uses the same live editor as Copy. Freeze that text
    # with the sent status so a pending autosave cannot leave a stale record.
    # Confirming an uncertain SMTP send instead preserves its reserved text.
    if (status == "sent" and save_draft and message.status in ("draft", "approved", "skipped")
            and not message.sent_at and message.send_state not in ("sending", "uncertain") and not message.delivery_uncertain):
        subject = subject.strip() or None
        if message.subject != subject or message.body != body:
            message.subject, message.body = subject, body
            message.edited = True
    try:
        set_message_status(db, message, status)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    labels = {
        "sent": "Marked as sent." + (" A follow-up is scheduled if there's no reply." if message.follow_up_due_at else ""),
        "replied": "Marked as replied. Follow-ups for this contact are cancelled.",
        "skipped": "Skipped.",
        "approved": "Approved.",
        "bounced": "Marked as bounced.",
        "draft": "Back to draft.",
    }
    return _panel_for_contact(request, db, message.contact,
                              {"ok": True, "message": labels.get(status, "Updated.")})


@router.post("/outreach/messages/{message_id}/send", response_class=HTMLResponse)
def send(
    message_id: uuid.UUID,
    request: Request,
    allow_guessed: bool = Form(False),
    retry_uncertain: bool = Form(False),
    save_draft: bool = Form(False),
    subject: str = Form(""),
    body: str = Form(""),
    db: Session = Depends(get_db),
):
    """
    Send a drafted email over SMTP.

    Synchronous on purpose: the user is waiting to find out whether it left, and
    a queued send that silently fails is worse than a slow button.
    """
    from app.services.outreach_sender import SendError, send_message

    message = _get_message(db, message_id)
    try:
        send_message(db, message, allow_guessed=allow_guessed, retry_uncertain=retry_uncertain,
                     draft={"subject": subject, "body": body} if save_draft else None)
    except SendError as exc:
        return _panel_for_contact(request, db, message.contact, {"ok": False, "message": str(exc)})
    except Exception as exc:
        logger.error("outreach: send failed for %s: %s", message_id, exc)
        db.rollback()
        return _panel_for_contact(request, db, message.contact,
                                  {"ok": False, "message": f"Send failed: {exc}"})
    return _panel_for_contact(
        request, db, message.contact,
        {"ok": True, "message": f"Sent to {message.contact.email}."},
    )


@router.post("/outreach/messages/{message_id}/delete", response_class=HTMLResponse)
def delete_message(message_id: uuid.UUID, request: Request, db: Session = Depends(get_db)):
    """Throw away a draft. Sent messages stay, since they are a record."""
    message = _get_message(db, message_id)
    db.refresh(message, with_for_update=True)
    if message.sent_at or message.status in ("sent", "replied", "bounced"):
        raise HTTPException(status_code=409, detail="A sent message can't be deleted.")
    if message.send_state in {"sending", "uncertain"} or message.delivery_uncertain:
        raise HTTPException(status_code=409, detail="Resolve this delivery before deleting the message.")
    contact = message.contact
    db.delete(message)
    db.commit()
    return _panel_for_contact(request, db, contact, {"ok": True, "message": "Draft deleted."})


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------

api_router = APIRouter(prefix="/api/apps", tags=["outreach"])


@api_router.post("/{app_id}/outreach", status_code=202)
def trigger_outreach(app_id: uuid.UUID, db: Session = Depends(get_db)):
    """Find contacts and draft openers for an application, inline."""
    app_obj = db.query(Application).filter(Application.id == app_id).first()
    if not app_obj:
        raise HTTPException(status_code=404, detail="Application not found")
    run_outreach(db, app_obj)
    return {"status": "ok"}


router.include_router(api_router)
