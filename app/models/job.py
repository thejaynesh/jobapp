import uuid
import enum
from datetime import datetime, timezone

from sqlalchemy import String, Boolean, Float, Text, DateTime, Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID, ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


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
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    experience_level: Mapped[str | None] = mapped_column(String, nullable=True)
    keyword_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    llm_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    llm_reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    matched_skills: Mapped[list] = mapped_column(ARRAY(String), default=list)
    missing_skills: Mapped[list] = mapped_column(ARRAY(String), default=list)
    status: Mapped[JobStatus] = mapped_column(
        SAEnum(JobStatus), default=JobStatus.new, nullable=False
    )
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    dedupe_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
