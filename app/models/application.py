import uuid
import enum
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import String, Boolean, Text, DateTime, Integer, Enum as SAEnum, ForeignKey, func, text, Index
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:  # relationship targets, resolved by SQLAlchemy at mapper config
    from app.models.outreach import Contact, OutreachMessage


class ApplicationStatus(enum.Enum):
    not_applied = "not_applied"
    applied = "applied"
    interviewing = "interviewing"
    offered = "offered"
    rejected = "rejected"
    withdrawn = "withdrawn"


class DocType(enum.Enum):
    resume = "resume"
    cover_letter = "cover_letter"


class Application(Base):
    __tablename__ = "applications"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jobs.id"), nullable=False
    )
    status: Mapped[ApplicationStatus] = mapped_column(
        SAEnum(ApplicationStatus), default=ApplicationStatus.not_applied, nullable=False
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # Superseded by the `contacts` table; kept so the pre-0012 discoveries that
    # were only ever written here stay readable.
    outreach_contacts: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))
    generation_status: Mapped[str] = mapped_column(String(20), nullable=False, default="idle", server_default="idle")
    generation_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # When the current run started. 'generating' with no clock on it cannot be
    # told apart from 'generating since a worker died on Tuesday' — both look
    # like work in progress forever. This is what the sweeper reads.
    generation_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # idle | discovering | done | failed — mirrors generation_status so the
    # outreach panel can poll while contact discovery runs on a worker.
    outreach_status: Mapped[str] = mapped_column(String(20), nullable=False, default="idle", server_default="idle")
    outreach_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    outreach_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    job = relationship("Job", backref="applications")
    documents: Mapped[list["ApplicationDocument"]] = relationship(
        "ApplicationDocument", back_populates="application"
    )
    contacts: Mapped[list["Contact"]] = relationship(
        "Contact",
        back_populates="application",
        cascade="all, delete-orphan",
        order_by="Contact.created_at",
    )
    outreach_messages: Mapped[list["OutreachMessage"]] = relationship(
        "OutreachMessage",
        back_populates="application",
        cascade="all, delete-orphan",
        order_by="OutreachMessage.created_at",
    )


class ApplicationDocument(Base):
    __tablename__ = "application_documents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("applications.id"), nullable=False
    )
    doc_type: Mapped[DocType] = mapped_column(SAEnum(DocType), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    path: Mapped[str] = mapped_column(String, nullable=False)
    generation_feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    generated_by: Mapped[str | None] = mapped_column(String, nullable=True)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    application: Mapped["Application"] = relationship(
        "Application", back_populates="documents"
    )
