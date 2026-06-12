import uuid
import enum
from datetime import datetime

from sqlalchemy import String, Boolean, Text, DateTime, Integer, Enum as SAEnum, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


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
    outreach_contacts: Mapped[list] = mapped_column(JSONB, default=list)

    job = relationship("Job", backref="application")
    documents: Mapped[list["ApplicationDocument"]] = relationship(
        "ApplicationDocument", back_populates="application"
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
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    application: Mapped["Application"] = relationship(
        "Application", back_populates="documents"
    )
