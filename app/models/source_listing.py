"""Exact source identities and revisions, without changing application/job IDs."""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class SourceListing(Base):
    __tablename__ = "source_listings"

    identity_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True, index=True)
    source: Mapped[str] = mapped_column(String, nullable=False)
    board: Mapped[str] = mapped_column(String, nullable=False, default="")
    external_id: Mapped[str | None] = mapped_column(String, nullable=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    details_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    upstream_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class ListingRevision(Base):
    __tablename__ = "listing_revisions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    listing_key: Mapped[str] = mapped_column(
        String(64), ForeignKey("source_listings.identity_key", ondelete="CASCADE"), nullable=False, index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # Changed stated fields and description hash/length; never duplicate the
    # entire corpus's description text for every poll.
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)


class FetchBoardRun(Base):
    __tablename__ = "fetch_board_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fetch_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String, nullable=False)
    board: Mapped[str] = mapped_column(String, nullable=False, default="")
    status: Mapped[str] = mapped_column(String, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observed_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    returned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    inserted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    merged: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    dropped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cursor: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error_category: Mapped[str | None] = mapped_column(String, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Committed before ingestion and cleared only after job changes commit.
    # A killed worker leaves a replayable batch, not lost network work.
    payload: Mapped[list | None] = mapped_column(JSONB, nullable=True)
