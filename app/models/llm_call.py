import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class LLMCall(Base):
    """
    One request to a language model, with what went in and what came back.

    Everything the app produces that is not a database row passes through here:
    a match score, a resume bullet, a cover letter. When one of those comes out
    wrong the question is always the same — was the prompt wrong, or was the
    reply wrong — and without the two stored side by side there is no way to
    tell them apart after the fact. The log lines that existed said a call
    happened and how it ended, which is the one thing you can already infer.

    `stage` is what makes it readable: a document generation is six calls with
    six different jobs, and "the resume is empty" is a question about exactly
    one of them.
    """

    __tablename__ = "llm_calls"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # What the call was for — "match", "resume_bullets", "cover_letter", ...
    stage: Mapped[str] = mapped_column(String(40), nullable=False, default="unknown")
    provider: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    model: Mapped[str] = mapped_column(String(160), nullable=False, default="")

    # The request exactly as sent, roles included. JSONB rather than text so a
    # system prompt that changed can be diffed against one that worked.
    messages: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    temperature: Mapped[float | None] = mapped_column(nullable=True)
    max_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)

    response: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Reasoning models put their working here and leave `response` empty when
    # the token ceiling runs out mid-thought — which is the difference between
    # "the model is bad at this" and "the budget was too small".
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    finish_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)

    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Plain columns, not foreign keys: the log outlives what it refers to, and a
    # cascade that deleted the evidence along with the job would defeat it.
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    application_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    __table_args__ = (
        Index("ix_llm_calls_created_at", created_at.desc()),
        Index("ix_llm_calls_stage_created", "stage", created_at.desc()),
        Index("ix_llm_calls_application", "application_id"),
        Index("ix_llm_calls_job", "job_id"),
    )
