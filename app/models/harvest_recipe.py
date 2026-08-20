"""
Payloads a site sent that we made nothing of, and what we learned to read them.

Two tables, one idea. `HarvestSample` is evidence — a bounded copy of a
response the shape-based walker could not turn into jobs. `HarvestRecipe` is
the conclusion — a declarative description of where that site keeps its jobs,
interpreted by our own code.

The recipe is data rather than generated code, and that is the load-bearing
decision. A recipe can be printed, diffed, run against a stored sample and
rejected before it ever touches the pipeline. A generated parser can be none of
those, and six months later nobody can say why it broke.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# proposed — written by the model, not yet trusted with anything
# active   — validated against real samples and in use
# rejected — failed validation, or superseded by a better one
RECIPE_STATUSES = ("proposed", "active", "rejected")


class HarvestSample(Base):
    """One payload a host sent, kept because we could not read it."""

    __tablename__ = "harvest_samples"
    __table_args__ = (
        Index("ix_harvest_samples_host_created", "host", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    host: Mapped[str] = mapped_column(String(160), nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    # Truncated on the way in. These are responses to a logged-in session and
    # can carry names and account identifiers, so this is a diagnostic sample
    # rather than an archive — see `services.harvest_samples`.
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # What the shape-based walker made of it. Zero is the case this exists for.
    found: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<HarvestSample {self.host} {self.bytes}B found={self.found}>"


class HarvestRecipe(Base):
    """How to read one host's payloads, learned from its samples."""

    __tablename__ = "harvest_recipes"
    __table_args__ = (
        Index("ix_harvest_recipes_host", "host"),
        # At most one active recipe per host: two would make extraction depend
        # on row order, which fails looking exactly like the site changing.
        Index(
            "uq_harvest_recipes_active", "host",
            unique=True, postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    host: Mapped[str] = mapped_column(String(160), nullable=False)
    recipe: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="proposed")

    # What validation saw when this was accepted, so a recipe that quietly
    # degrades can be compared against the day it earned its place.
    jobs_found: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    samples_tried: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    activated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<HarvestRecipe {self.host} {self.status}>"
