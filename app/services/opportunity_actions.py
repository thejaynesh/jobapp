"""Read-only networking actions for the same daily budget as applications."""
from datetime import datetime, timezone

from sqlalchemy import or_
from sqlalchemy.orm import joinedload

from app.models.application import Application, ApplicationStatus
from app.models.outreach import Contact, NetworkPerson, OutreachConversation, OutreachMessage
from app.services.tunables import value


def build(db, profile, now=None):
    now = now or datetime.now(timezone.utc)
    minutes = int(value(profile, "plan_networking_minutes"))
    actions, seen = [], set()
    conversations = db.query(OutreachConversation).join(NetworkPerson).options(
        joinedload(OutreachConversation.person), joinedload(OutreachConversation.application).joinedload(Application.job)
    ).filter(OutreachConversation.status != "closed", NetworkPerson.do_not_contact.is_(False),
        or_(OutreachConversation.snoozed_until.is_(None), OutreachConversation.snoozed_until <= now),
        or_(OutreachConversation.status == "reply_needed", OutreachConversation.next_action_due_at <= now,
            (OutreachConversation.status == "snoozed") & (OutreachConversation.snoozed_until <= now)),
    ).order_by(OutreachConversation.next_action_due_at.asc().nullsfirst(), OutreachConversation.last_activity_at).limit(40).all()
    for conversation in conversations:
        reply = conversation.status == "reply_needed"
        review = conversation.status == "ready"
        name = conversation.person.name or conversation.person.primary_email or "your contact"
        actions.append({"kind": "reply" if reply else "review_outreach" if review else "network_followup", "title": conversation.next_action or f"{'Reply to' if reply else 'Review draft for' if review else 'Follow up with'} {name}",
            "company": conversation.company, "job": conversation.application.job if conversation.application else None,
            "url": f"/outreach/conversations/{conversation.id}", "minutes": minutes, "priority": 1100 if reply else 105 if review else 990,
            "reason": f"{name} replied; review the conversation before responding." if reply else "A draft is ready. Check the person, context and request before sending." if review else f"Your saved next step with {name} is due.",
            "evidence": [], "question_id": ""})
        seen.add(conversation.id)

    drafts = db.query(OutreachMessage).join(Contact).outerjoin(OutreachConversation).outerjoin(
        NetworkPerson, Contact.person_id == NetworkPerson.id).outerjoin(
        Application, OutreachMessage.application_id == Application.id).options(joinedload(OutreachMessage.contact)).filter(
        OutreachMessage.status.in_(("draft", "approved")), Contact.archived.is_(False),
        or_(NetworkPerson.id.is_(None), NetworkPerson.do_not_contact.is_(False)),
        or_(OutreachConversation.id.is_(None), OutreachConversation.status.notin_(("closed", "snoozed"))),
        or_(Application.id.is_(None), Application.status.notin_((ApplicationStatus.rejected, ApplicationStatus.withdrawn)),
            OutreachMessage.kind.in_(("reply", "thank_you"))),
    ).order_by(OutreachMessage.created_at, OutreachMessage.id).limit(50).all()
    for message in drafts:
        identity = message.conversation_id or message.contact_id
        if identity in seen:
            continue
        seen.add(identity)
        actions.append({"kind": "review_outreach", "title": f"Review message to {message.contact.display_name}",
            "company": message.contact.company, "job": None, "url": f"/outreach/contacts/{message.contact_id}",
            "minutes": minutes, "priority": 105,
            "reason": "A draft is ready. Check the person, context and request before sending.", "evidence": [], "question_id": ""})
    return actions
