import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

# Vocabularies are plain strings rather than PG enums: outreach grows new
# channels and message kinds far more often than the job pipeline grows
# statuses, and a new value shouldn't need a type migration.

# Where a contact came from.
CONTACT_SOURCES = ("hunter", "linkedin", "description", "pattern", "manual")

# What the contact is to us, which decides how a message is pitched.
CONTACT_ROLES = ("recruiter", "hiring_manager", "engineer", "executive", "generic", "unknown")

# How much we trust `Contact.email`.
EMAIL_STATUSES = ("verified", "accept_all", "guessed", "unverified", "invalid", "unknown")

MESSAGE_CHANNELS = ("email", "linkedin", "linkedin_note", "twitter")
MESSAGE_KINDS = ("initial", "follow_up", "referral_request", "thank_you", "reconnect")

# draft     — written, not reviewed
# approved  — user has read it and is happy to send
# sent      — left the building (SMTP, or the user sent it by hand)
# replied   — they answered; stops the follow-up sequence
# bounced   — delivery failed
# skipped   — user decided against sending it
MESSAGE_STATUSES = ("draft", "approved", "sent", "replied", "bounced", "skipped")

# Statuses that mean the message is finished with, one way or another.
CLOSED_MESSAGE_STATUSES = ("replied", "bounced", "skipped")


class Contact(Base):
    """
    A person worth talking to about a job.

    Scoped to an application, not to a company: the same recruiter approached
    about two different roles is two conversations, each with its own thread and
    its own follow-up clock. `company_key` is carried alongside so a normalized
    employer name ("Acme, Inc." and "Acme Inc" are one company) can still group
    them for a cross-application view.
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
    application_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("applications.id", ondelete="CASCADE"), nullable=True
    )

    company: Mapped[str] = mapped_column(String, nullable=False)
    # Normalized company name (see services.company_domain.company_key) — the
    # join key for reuse, since "Acme, Inc." and "Acme Inc" are one employer.
    company_key: Mapped[str] = mapped_column(String, nullable=False)
    domain: Mapped[str | None] = mapped_column(String, nullable=True)

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
        return bool(self.email or self.linkedin_url)


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
        UUID(as_uuid=True), ForeignKey("applications.id", ondelete="CASCADE"), nullable=True
    )

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

    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    follow_up_due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    send_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=True
    )

    contact = relationship("Contact", back_populates="messages")
    application = relationship("Application", back_populates="outreach_messages")

    @property
    def is_open(self) -> bool:
        """Still in play — not replied to, bounced, or abandoned."""
        return self.status not in CLOSED_MESSAGE_STATUSES

    @property
    def char_count(self) -> int:
        return len(self.body or "")
