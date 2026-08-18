import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Index, String, func
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ArchivedJob(Base):
    """
    A job that was rejected long enough ago to stop carrying its description.

    The `jobs` table is mostly descriptions, and most of those belong to
    postings the pipeline said no to months back. Nothing reads them again: a
    job filtered on a title mismatch in June is not going to be reconsidered,
    and the row costs a paragraph of text apiece across a hundred thousand of
    them.

    What is emphatically *not* disposable is the fact that we have seen it.
    Deduplication has three layers — the URL, the source's own id, and the
    content hash — and dropping a job row silently defeats all three: the next
    fetch re-inserts the same posting as brand new, spends a scoring call on
    it, and reaches the same verdict. Then does it again next week.

    So this table keeps exactly the columns those three layers read, and
    `deduplication.find_existing_job` checks it. Everything else — the
    description, the reasoning, the skills, the score history — goes. The row
    is a tombstone, and its whole job is to be found.
    """

    __tablename__ = "archived_jobs"

    # The job's original id, carried over rather than regenerated: anything
    # that still holds a reference (the LLM log keeps job_id as a plain column
    # on purpose) can at least be traced to this row.
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)

    # ---- What deduplication reads. Nothing here is optional. --------------
    source: Mapped[str] = mapped_column(String, nullable=False)
    source_job_id: Mapped[str | None] = mapped_column(String, nullable=True)
    source_urls: Mapped[list] = mapped_column(ARRAY(String), default=list)
    url: Mapped[str] = mapped_column(String, nullable=False)
    # Unique for the same reason it is unique on `jobs`: two rows claiming the
    # same posting would make "have we seen this?" ambiguous.
    dedupe_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)

    # ---- What makes the row readable by a human ---------------------------
    title: Mapped[str] = mapped_column(String, nullable=False)
    company: Mapped[str] = mapped_column(String, nullable=False)
    location: Mapped[str | None] = mapped_column(String, nullable=True)
    # Kept so the funnel's "why were jobs dropped" breakdown does not quietly
    # lose a hundred thousand rows the day this first runs.
    filter_reason: Mapped[str | None] = mapped_column(
        String, nullable=True, index=True
    )

    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    posted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    archived_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        # The dedupe lookup, which runs once per fetched posting and is the
        # only hot path on this table.
        Index("ix_archived_jobs_url", "url"),
        Index("ix_archived_jobs_source_job", "source", "source_job_id"),
        Index("ix_archived_jobs_archived_at", archived_at.desc()),
        # GIN, because the first dedupe layer asks "is this URL in the array",
        # which a btree cannot answer and which runs once per fetched posting.
        Index("ix_archived_jobs_source_urls", "source_urls",
              postgresql_using="gin"),
    )
