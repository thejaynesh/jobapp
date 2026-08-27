"""
How to walk a board, learned rather than hand-written.

The sibling of `harvest_recipe`. That one answers "where are the jobs in this
payload"; this answers the question before it — "how do I get the page to show
me more of them".

`CrawlSample` is evidence: what a page offered when a visit failed to get past
the first screenful. `CrawlRecipe` is the conclusion: which of the three ways
this board reaches its second page, and what that way needs.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# Same three states as a harvest recipe, and the same meanings.
CRAWL_STATUSES = ("proposed", "active", "rejected")

# The three ways a board reaches its second page.
#
#   scroll — there is no second page; the list grows as you move down it
#   click  — numbered controls, one address for every page
#   url    — the page number is a query parameter
#
# Closed set on purpose. A model asked for free text here would eventually
# answer "infinite-scroll" or "pagination" or "buttons at the bottom", and
# three spellings of one mode is a bug that only shows up on the board that
# spelled it differently.
CRAWL_MODES = ("scroll", "click", "url")


class CrawlSample(Base):
    """What a page offered, when a visit could not get past its first screen."""

    __tablename__ = "crawl_samples"
    __table_args__ = (
        Index("ix_crawl_samples_host_created", "host", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    host: Mapped[str] = mapped_column(String(160), nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    # Candidate controls, scroll behaviour, existing query parameters. Trimmed
    # by the extension before it leaves the browser: this is a description of
    # the navigation, not a copy of the document.
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False)
    pages_reached: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    batches: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        # See the migration: now() is transaction-start time, which makes
        # "newest first" arbitrary among rows written together.
        DateTime(timezone=True), nullable=False,
        server_default=text("clock_timestamp()"),
    )


class CrawlRecipe(Base):
    """One board's navigation, as data rather than as code."""

    __tablename__ = "crawl_recipes"
    __table_args__ = (
        Index("ix_crawl_recipes_host", "host"),
        Index("uq_crawl_recipes_active", "host", unique=True,
              postgresql_where=text("status = 'active'")),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    host: Mapped[str] = mapped_column(String(160), nullable=False)
    recipe: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="proposed"
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # How the visits made under this recipe have gone. A recipe can validate
    # against the sample it was written from and still be wrong in the wild —
    # the sample is a snapshot of the page, not of the outcome — so the outcome
    # is recorded and a recipe that never gets anywhere retires itself.
    tries: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    best_pages: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        # See the migration: now() is transaction-start time, which makes
        # "newest first" arbitrary among rows written together.
        DateTime(timezone=True), nullable=False,
        server_default=text("clock_timestamp()"),
    )
    activated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
