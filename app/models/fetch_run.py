import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class FetchRun(Base):
    """
    One fetch cycle, kept so the last several are inspectable.

    The profile only ever held the most recent run, which makes the interesting
    questions unanswerable: is LinkedIn newly broken or has it been dead for a
    week? Did that source always return duplicates? Was yesterday's cycle
    slower? History is what turns a number into a signal.
    """

    __tablename__ = "fetch_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    # ok | partial | failed
    status: Mapped[str] = mapped_column(String, nullable=False, default="ok")
    # Which slice of the pipeline this run was: all | api | boards | browser.
    # One task used to fetch everything, so a cheap API source that could
    # refresh hourly waited on a browser tier that only needs to run twice a
    # day. Each group now runs on its own schedule, and this is what makes a
    # run's numbers comparable to the right other runs.
    group: Mapped[str] = mapped_column(
        "group", String, nullable=False, default="all", index=True
    )

    fetched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    inserted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    merged: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stale: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # What the cycle actually searched for — worth keeping, since query
    # expansion changes it and that changes everything downstream.
    queries: Mapped[list] = mapped_column(ARRAY(String), default=list)
    locations: Mapped[list] = mapped_column(ARRAY(String), default=list)

    links_attempted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    links_resolved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    links_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    boards_polled: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    boards_discovered: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    boards_sniffed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # One-time backfill report, on the cycle that ran it.
    backfill: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    sources: Mapped[list["FetchSourceRun"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", lazy="selectin"
    )


class FetchSourceRun(Base):
    """What one source did during one cycle."""

    __tablename__ = "fetch_source_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fetch_runs.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    source: Mapped[str] = mapped_column(String, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # ok | partial | failed | empty | disabled
    status: Mapped[str] = mapped_column(String, nullable=False, default="ok")

    fetched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Fetched counts what the source returned; these say whether any of it was
    # worth having. A source can return 200 jobs and contribute nothing new.
    inserted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    merged: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stale: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    errors: Mapped[list] = mapped_column(ARRAY(String), default=list)

    run: Mapped["FetchRun"] = relationship(back_populates="sources")
