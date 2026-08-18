import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    DateTime, Float, ForeignKey, Index, Integer, String, Text, func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class JobScore(Base):
    """
    One evaluation of one job, kept after the next one replaces it.

    Every column matching writes to the job — score, reasoning, filter reason —
    is a single slot that the next evaluation overwrites. That was tolerable
    while a job was scored once and never again. It isn't now: enrichment sends
    a job back to be re-scored the moment its description grows, so a job that
    was rejected at 45 on a 500-character stub and accepted at 82 on the real
    posting shows only the 82, and the evidence that the pipeline changed its
    mind — the part worth reading — is gone.

    So each evaluation appends a row here before the job row is overwritten.
    The point is not audit for its own sake. It answers three questions that
    nothing else can: whether enrichment actually changes verdicts or just
    costs calls, whether the second-opinion pass moves scores enough to earn
    what it costs, and — when a job looks mis-scored — what it was scored on.
    `description_chars` is what makes the last one answerable: "45, judged on
    500 characters" and "45, judged on 6,200" are different failures.

    The LLM log is not this. It stores the model's raw reply for a couple of
    thousand recent calls and is pruned on a timer; this stores the decision,
    per job, and is pruned per job — so the first evaluation of a job survives
    however many thousand calls the pipeline has made since.
    """

    __tablename__ = "job_scores"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # A real foreign key, unlike the LLM log's plain columns: that log outlives
    # what it refers to on purpose, and this is a property of the job — a row
    # about a deleted job has nothing left to explain.
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # Stamped in Python, not by `now()`. Postgres' `now()` is the transaction's
    # start time, so two evaluations of the same job inside one transaction get
    # identical timestamps — and this table's only ordering is chronological,
    # which would then be a coin toss between "was 45, now 82" and the reverse.
    # The server default stays for anything inserted outside the ORM.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, server_default=func.now(),
        nullable=False,
    )

    # The number this evaluation decided on — the deep score where there was
    # one, the first otherwise. Null when the keyword filter rejected the job
    # before any model saw it, which is itself the fact worth recording.
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    llm_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    llm_score_deep: Mapped[float | None] = mapped_column(Float, nullable=True)
    matched_by: Mapped[str | None] = mapped_column(String, nullable=True)
    deep_matched_by: Mapped[str | None] = mapped_column(String, nullable=True)

    # The job status this evaluation left behind, and — when it rejected the
    # job — why. Stored as the plain string rather than the enum so a future
    # rename of a status can't make old history unreadable.
    status: Mapped[str] = mapped_column(String, nullable=False, default="")
    filter_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    filter_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The accept threshold in force at the time. Without it, a row reading
    # "scored 58, filtered out" becomes nonsense the day the user moves their
    # minimum to 50 — the history would look like the pipeline misfiring.
    min_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    # How much description this verdict was reached on.
    description_chars: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    # initial | description_grew | rescored
    trigger: Mapped[str] = mapped_column(String, nullable=False, default="initial")

    __table_args__ = (
        # The one query this table serves: a job's evaluations, newest first.
        Index("ix_job_scores_job_created", "job_id", created_at.desc()),
    )

    @property
    def trigger_label(self) -> str:
        return {
            "initial": "first evaluation",
            "description_grew": "re-scored on a fuller description",
            "rescored": "re-scored",
        }.get(self.trigger, self.trigger or "evaluated")
