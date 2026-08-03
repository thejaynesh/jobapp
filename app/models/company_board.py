import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class CompanyBoard(Base):
    """
    A company's ATS board that we know how to poll directly.

    Boards arrive from several places — the user's config, the verified seed
    list, links spotted in fetched postings, community job lists, apply URLs
    resolved out of aggregator redirects, and careers pages we sniffed — and
    the registry keeps them all in one place with enough history to rank them.
    Boards that keep coming back empty get retired so the per-cycle budget goes
    to the ones actually producing jobs.
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

    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_fetched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
