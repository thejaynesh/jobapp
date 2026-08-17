import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class EnrichmentRun(Base):
    """
    One enrichment pass, recorded the way fetch cycles are.

    Enrichment is the pipeline's only self-correcting step: everything else
    stores what a source gave it, and this goes back for what the source left
    out. That makes "is it working?" a question with a lot of parts — which
    method won, how much text was actually gained, how many jobs got a second
    chance at matching, and which hosts are refusing us. A single log line
    answers none of them; a row per run answers all of them over time.
    """

    __tablename__ = "enrichment_runs"

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

    attempted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    enriched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unchanged: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Which method got there first. The order they are tried in is cheapest and
    # most reliable first, so a run where the LLM does most of the work is
    # telling you the ATS patterns have gone stale.
    via_ats_api: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    via_json_ld: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    via_llm: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    via_landing_html: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    queued_browser: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # The number the whole feature exists for.
    chars_gained: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Jobs that were filtered out for having no description and are now back in
    # the matching queue with a real one.
    requeued_for_matching: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )

    # host → count. Which sites are refusing us is the thing that decides
    # whether a host belongs on the browser tier instead.
    failures_by_host: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
