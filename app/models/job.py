import re
import uuid
import enum
from datetime import datetime, timezone

from sqlalchemy import String, Boolean, Float, Text, DateTime, Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID, ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# Words too generic to be worth blocking a title over — offering "Block
# 'and'" would be noise where the real candidates ("Manager", "Clearance",
# "Embedded") are the point.
_TITLE_WORD_NOISE = frozenset({
    "and", "for", "the", "with", "all", "our", "you", "your", "job", "role",
    "level", "levels", "remote", "hybrid", "onsite", "time", "full", "part",
})


class JobStatus(enum.Enum):
    new = "new"
    filtered_out = "filtered_out"
    matched = "matched"
    docs_generated = "docs_generated"


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source: Mapped[str] = mapped_column(String, nullable=False)
    source_job_id: Mapped[str | None] = mapped_column(String, nullable=True)
    source_urls: Mapped[list] = mapped_column(ARRAY(String), default=list)
    title: Mapped[str] = mapped_column(String, nullable=False)
    company: Mapped[str] = mapped_column(String, nullable=False)
    location: Mapped[str | None] = mapped_column(String, nullable=True)
    is_remote: Mapped[bool] = mapped_column(Boolean, default=False)
    url: Mapped[str] = mapped_column(String, nullable=False)
    # The employer's own apply link, when `url` was an aggregator redirect page
    # that we followed through to the company's ATS / careers site.
    apply_url: Mapped[str | None] = mapped_column(String, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    experience_level: Mapped[str | None] = mapped_column(String, nullable=True)
    keyword_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    llm_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    llm_reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    matched_by: Mapped[str | None] = mapped_column(String, nullable=True)
    matched_skills: Mapped[list] = mapped_column(ARRAY(String), default=list)
    missing_skills: Mapped[list] = mapped_column(ARRAY(String), default=list)
    status: Mapped[JobStatus] = mapped_column(
        SAEnum(JobStatus), default=JobStatus.new, nullable=False
    )
    # Why this job was filtered out. `filter_reason` is a stable key for
    # grouping (see matcher.FILTER_REASON_LABELS); `filter_detail` is the
    # sentence naming the actual values that triggered it.
    filter_reason: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    filter_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    # What the posting said about visa sponsorship, quoted. Advisory only: it is
    # displayed beside the job and never filters, scores or ranks it, and never
    # reaches an LLM. `direction` is "negative" or "positive".
    sponsorship_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    sponsorship_direction: Mapped[str | None] = mapped_column(String, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    posted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    dedupe_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    # Liveness: whether the posting is still up on the employer's side. A job
    # applied to three weeks after it closed is wasted effort, and nothing else
    # in the pipeline ever looks at a posting again once it is stored.
    # `closed_at` set means a check found it gone; `closed_note` says how it
    # knew (HTTP 404, a "no longer accepting applications" banner, ...).
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    closed_note: Mapped[str | None] = mapped_column(String, nullable=True)
    liveness_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # When the stored description was replaced by a meaningfully fuller one
    # (a cross-post merge, a harvest, an enrichment pass). Documents generated
    # before this moment were grounded in the thinner text, which is what the
    # "docs predate a fuller description" nudge reads.
    description_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    @property
    def title_block_candidates(self) -> list[str]:
        """
        Words of this title worth offering as a personal blocklist entry.

        The "not interested" menu renders these as one-click choices, because
        the user picking the word beats the system guessing which part of
        "Embedded Software Manager" they never want to see again.
        """
        seen: set[str] = set()
        out: list[str] = []
        for word in re.findall(r"[A-Za-z][A-Za-z+#.]{2,}", self.title or ""):
            low = word.lower()
            if low in _TITLE_WORD_NOISE or low in seen:
                continue
            seen.add(low)
            out.append(word)
        return out[:8]
