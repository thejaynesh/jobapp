"""Employer identities shared by watched boards, opportunities and contacts."""
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    domain: Mapped[str | None] = mapped_column(String(253), nullable=True, unique=True)
    domain_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    identity_source: Mapped[str] = mapped_column(String(30), nullable=False, default="job", server_default="job")
    aliases: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    evidence: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    careers_url: Mapped[str | None] = mapped_column(Text)
    watched: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    research_status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending", server_default="pending")
    research_note: Mapped[str | None] = mapped_column(Text)
    last_researched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_refresh_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
