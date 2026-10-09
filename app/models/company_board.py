import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, UniqueConstraint, ForeignKey
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class CompanyBoard(Base):
    """
    A company's ATS board that we know how to poll directly.

    Boards arrive from several places — the user's config, the verified seed
    list, links spotted in fetched postings, community job lists, apply URLs
    resolved out of aggregator redirects, and careers pages we sniffed — and
    the registry keeps them all in one place with enough history to rank them.
    Quiet boards receive less frequent probes; only repeated confirmed missing
    endpoints retire automatically.
    """

    __tablename__ = "company_boards"
    __table_args__ = (UniqueConstraint("ats", "slug", name="uq_company_boards_ats_slug"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # greenhouse | lever | ashby | smartrecruiters | workable | recruitee | workday
    ats: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # Company slug; for Workday the "tenant:host:site" triple.
    slug: Mapped[str] = mapped_column(String, nullable=False)
    company: Mapped[str | None] = mapped_column(String, nullable=True)
    # configured | seed | discovered | harvested | resolved | sniffed
    origin: Mapped[str] = mapped_column(String, nullable=False, default="discovered")
    # Careers host this board was sniffed from, when that's how we found it.
    source_host: Mapped[str | None] = mapped_column(String, nullable=True)

    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    consecutive_empty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_job_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_job_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # When this board was last probed to confirm it exists, and what the probe
    # found if it didn't. A board discovered from a link is a guess until
    # something asks its ATS whether the slug is real — `greenhouse/linkedin`
    # and `greenhouse/appcast` were both polled for months on that guess.
    validated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    inactive_reason: Mapped[str | None] = mapped_column(String, nullable=True)

    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_fetched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    consecutive_not_found: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_new_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    fetch_cursor: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="SET NULL"), nullable=True, index=True)
