import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

# Vocabularies are plain strings rather than PG enums: outreach grows new
# channels and message kinds far more often than the job pipeline grows
# statuses, and a new value shouldn't need a type migration.

# Where a contact came from.
CONTACT_SOURCES = ("hunter", "linkedin", "description", "pattern", "manual", "github", "team_page", "browser")

# What the contact is to us, which decides how a message is pitched.
CONTACT_ROLES = ("recruiter", "hiring_manager", "engineer", "executive", "generic", "unknown")

# How much we trust `Contact.email`.
EMAIL_STATUSES = ("verified", "accept_all", "guessed", "unverified", "invalid", "unknown")

MESSAGE_CHANNELS = ("email", "linkedin", "linkedin_note", "twitter")
MESSAGE_KINDS = ("initial", "follow_up", "reply", "referral_request", "thank_you", "reconnect")

# draft     — written, not reviewed
# approved  — user has read it and is happy to send
# sent      — left the building (SMTP, or the user sent it by hand)
# replied   — they answered; stops the follow-up sequence
# bounced   — delivery failed
# skipped   — user decided against sending it
MESSAGE_STATUSES = ("draft", "approved", "sent", "replied", "bounced", "skipped")

# Statuses that mean the message is finished with, one way or another.
CLOSED_MESSAGE_STATUSES = ("replied", "bounced", "skipped")

CONVERSATION_STATUSES = (
    "researching", "ready", "awaiting_reply", "reply_needed", "intro_requested",
    "introduced", "referral_offered", "referral_submitted", "snoozed", "closed",
)
RELATIONSHIP_KINDS = ("unknown", "alumni", "colleague", "friend", "introduced")


class NetworkPerson(Base):
    """A durable identity shared by contacts for different applications."""
    __tablename__ = "network_people"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str | None] = mapped_column(String)
    primary_email: Mapped[str | None] = mapped_column(String, unique=True)
    linkedin_url: Mapped[str | None] = mapped_column(String, unique=True)
    relationship_kind: Mapped[str] = mapped_column(String, default="unknown", server_default="unknown")
    relationship_notes: Mapped[str | None] = mapped_column(Text)
    do_not_contact: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    contacts: Mapped[list["Contact"]] = relationship("Contact", back_populates="person")
    conversations: Mapped[list["OutreachConversation"]] = relationship("OutreachConversation", back_populates="person")


class OutreachConversation(Base):
    """One person's company conversation, independent of any single role."""
    __tablename__ = "outreach_conversations"
    __table_args__ = (
        UniqueConstraint("person_id", "company_key", name="uq_outreach_conversation_person_company"),
        Index("ix_outreach_conversations_next_action_due_at", "next_action_due_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    person_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("network_people.id", ondelete="CASCADE"))
    company_key: Mapped[str] = mapped_column(String)
    company: Mapped[str] = mapped_column(String)
    application_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("applications.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String, default="researching", server_default="researching")
    next_action: Mapped[str | None] = mapped_column(Text)
    next_action_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    person: Mapped["NetworkPerson"] = relationship("NetworkPerson", back_populates="conversations")
    application = relationship("Application")
    messages: Mapped[list["OutreachMessage"]] = relationship("OutreachMessage", back_populates="conversation", order_by="OutreachMessage.created_at")
    interactions: Mapped[list["OutreachInteraction"]] = relationship("OutreachInteraction", back_populates="conversation", order_by="OutreachInteraction.occurred_at")


class OutreachInteraction(Base):
    """Inbound replies and human-recorded relationship milestones."""
    __tablename__ = "outreach_interactions"
    __table_args__ = (Index("ix_outreach_interactions_conversation_id", "conversation_id"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("outreach_conversations.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String)
    body: Mapped[str] = mapped_column(Text, default="", server_default="")
    message_id: Mapped[str | None] = mapped_column(String, unique=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    conversation: Mapped["OutreachConversation"] = relationship("OutreachConversation", back_populates="interactions")


class ContactDiscoveryCache(Base):
    """Reusable source evidence, including explicit empty and failed outcomes."""
    __tablename__ = "contact_discovery_cache"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    company_key: Mapped[str] = mapped_column(String, index=True)
    source: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    data: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    error: Mapped[str | None] = mapped_column(Text)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Contact(Base):
    """
    A person worth talking to about a job.

    A discovery record for an application, or a standalone contact. Person and
    conversation references preserve relationship history across multiple roles.
    """

    __tablename__ = "contacts"
    __table_args__ = (
        # NULL emails compare as distinct in Postgres, so several unnamed
        # contacts on one application are fine; a duplicate address is not.
        UniqueConstraint("application_id", "email", name="uq_contacts_application_email"),
        Index("ix_contacts_company_key", "company_key"),
        Index("ix_contacts_application_id", "application_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # The application the contact was discovered for. Nullable so a contact can
    # outlive the application, and so contacts can be added company-wide.
    #
    # SET NULL, not CASCADE. Nullable is not the same as surviving: CASCADE
    # deleted the contact along with the application, which is the opposite of
    # what the line above promises. A contact is a person at a company and
    # keeps being one after an application is gone.
    application_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("applications.id", ondelete="SET NULL"),
        nullable=True,
    )

    company: Mapped[str] = mapped_column(String, nullable=False)
    # Normalized company name (see services.company_domain.company_key) — the
    # join key for reuse, since "Acme, Inc." and "Acme Inc" are one employer.
    company_key: Mapped[str] = mapped_column(String, nullable=False)
    domain: Mapped[str | None] = mapped_column(String, nullable=True)
    company_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("companies.id", ondelete="SET NULL"))
    person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("network_people.id", ondelete="SET NULL"), index=True)
    evidence: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    person: Mapped["NetworkPerson | None"] = relationship("NetworkPerson", back_populates="contacts")

    name: Mapped[str | None] = mapped_column(String, nullable=True)
    first_name: Mapped[str | None] = mapped_column(String, nullable=True)
    last_name: Mapped[str | None] = mapped_column(String, nullable=True)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    department: Mapped[str | None] = mapped_column(String, nullable=True)
    role: Mapped[str] = mapped_column(String, nullable=False, default="unknown")

    email: Mapped[str | None] = mapped_column(String, nullable=True)
    email_status: Mapped[str] = mapped_column(String, nullable=False, default="unknown")
    # 0-100. Hunter's own confidence where it gives one, otherwise our estimate
    # for a pattern-derived address.
    email_confidence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Other addresses that could reach this person, best first — pattern guesses
    # kept around so a bounce has somewhere to fall back to.
    alternate_emails: Mapped[list] = mapped_column(
        ARRAY(String), nullable=False, default=list, server_default="{}"
    )

    linkedin_url: Mapped[str | None] = mapped_column(String, nullable=True)
    # Any other public profile — a GitHub page, a personal site.
    profile_url: Mapped[str | None] = mapped_column(String, nullable=True)
    twitter: Mapped[str | None] = mapped_column(String, nullable=True)
    phone: Mapped[str | None] = mapped_column(String, nullable=True)

    source: Mapped[str] = mapped_column(String, nullable=False, default="manual")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=True
    )

    application = relationship("Application", back_populates="contacts")
    messages: Mapped[list["OutreachMessage"]] = relationship(
        "OutreachMessage",
        back_populates="contact",
        cascade="all, delete-orphan",
        order_by="OutreachMessage.created_at",
    )

    @property
    def display_name(self) -> str:
        return self.name or self.email or "Unnamed contact"

    @property
    def is_reachable(self) -> bool:
        return bool(self.email or self.linkedin_url or self.profile_url or self.twitter)


class OutreachMessage(Base):
    """
    One drafted (and possibly sent) message to a contact.

    A contact accumulates a sequence: an initial message, then follow-ups at the
    intervals in OUTREACH_FOLLOWUP_DAYS. `sequence_step` orders them and
    `follow_up_due_at` is what the scheduler looks at — it is cleared once the
    next step exists, so a message is only ever queued for one follow-up.
    """

    __tablename__ = "outreach_messages"
    __table_args__ = (
        Index("ix_outreach_messages_contact_id", "contact_id"),
        Index("ix_outreach_messages_application_id", "application_id"),
        Index("ix_outreach_messages_status", "status"),
        Index("ix_outreach_messages_follow_up_due_at", "follow_up_due_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    application_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("applications.id", ondelete="SET NULL"), nullable=True
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("outreach_conversations.id", ondelete="SET NULL"), index=True)
    conversation: Mapped["OutreachConversation | None"] = relationship("OutreachConversation", back_populates="messages")
    followup_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    followup_error: Mapped[str | None] = mapped_column(Text)

    channel: Mapped[str] = mapped_column(String, nullable=False, default="email")
    kind: Mapped[str] = mapped_column(String, nullable=False, default="initial")
    sequence_step: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    tone: Mapped[str] = mapped_column(String, nullable=False, default="warm")

    subject: Mapped[str | None] = mapped_column(String, nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")

    status: Mapped[str] = mapped_column(String, nullable=False, default="draft")
    # Regeneration instructions the user gave for the current body.
    feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Provider/model labels that wrote it, or NULL when a template did.
    generated_by: Mapped[str | None] = mapped_column(String, nullable=True)
    # True once a human edited the body, so regeneration warns before clobbering.
    edited: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # The RFC 5322 Message-ID this went out with. Kept because a reply quotes it
    # in In-Reply-To/References, which makes "they answered" a header match
    # rather than a guess about who a mail is from and what it is about.
    message_id: Mapped[str | None] = mapped_column(String, nullable=True)

    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    follow_up_due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    send_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    send_state: Mapped[str] = mapped_column(String, nullable=False, default="idle", server_default="idle")
    send_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=True
    )

    contact = relationship("Contact", back_populates="messages")
    application = relationship("Application", back_populates="outreach_messages")

    @property
    def send_in_progress(self) -> bool:
        if self.send_state != "sending" or self.send_started_at is None:
            return False
        started = self.send_started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        return started > datetime.now(timezone.utc) - timedelta(minutes=10)

    @property
    def delivery_uncertain(self) -> bool:
        return (self.send_state in ("sending", "uncertain") and not self.send_in_progress
                or self.status in ("draft", "approved") and bool(self.message_id)
                and not self.send_error and self.send_state == "idle")

    @property
    def is_open(self) -> bool:
        """Still in play — not replied to, bounced, or abandoned."""
        return self.status not in CLOSED_MESSAGE_STATUSES

    @property
    def char_count(self) -> int:
        return len(self.body or "")
