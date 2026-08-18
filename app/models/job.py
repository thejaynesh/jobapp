import re
import uuid
import enum
from datetime import datetime, timezone

from sqlalchemy import String, Boolean, Float, Text, DateTime, Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID, ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

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
    # A second opinion, from the strongest configured provider, on the jobs
    # whose first score landed in the band where accept and reject flip. Kept
    # alongside the first rather than replacing it: comparing the two is the
    # only way to learn whether the second pass earns what it costs.
    llm_score_deep: Mapped[float | None] = mapped_column(Float, nullable=True)
    llm_reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    matched_by: Mapped[str | None] = mapped_column(String, nullable=True)
    deep_matched_by: Mapped[str | None] = mapped_column(String, nullable=True)
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

    # What the posting states, read out of the description once instead of
    # re-derived from prose on every scoring call. All nullable, and null means
    # "the posting doesn't say" — never a guess. A guessed salary is worse than
    # a missing one, because the salary filter would then drop jobs on a number
    # nobody ever wrote down.
    salary_min: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    salary_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    salary_currency: Mapped[str | None] = mapped_column(String, nullable=True)
    # full_time | part_time | contract | internship
    employment_type: Mapped[str | None] = mapped_column(String, nullable=True)
    required_years: Mapped[float | None] = mapped_column(Float, nullable=True)
    required_skills: Mapped[list] = mapped_column(ARRAY(String), default=list)
    nice_to_have_skills: Mapped[list] = mapped_column(ARRAY(String), default=list)
    education_required: Mapped[str | None] = mapped_column(String, nullable=True)
    benefits_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # ISO code of the posting's own language. German Arbeitnow listings waste a
    # matcher call apiece; knowing which they are is what lets them be skipped.
    language: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    details_extracted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # When enrichment last went looking for a fuller description — whether or
    # not it found one. Without this a job that cannot be enriched is picked
    # again by every pass forever: nothing about it changes, so it stays at the
    # front of the queue and the real backlog behind it is never reached.
    #
    # A stamp rather than a flag, so a host that was down in March is retried
    # in April instead of being written off. See `ENRICH_RETRY_DAYS`.
    enrichment_attempted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    # Every verdict this job has been given, newest first. The columns above
    # hold only the latest one, and a job re-scored on a fuller description
    # would otherwise show no sign that it was ever judged differently.
    #
    # Eagerly loaded because the list page renders it on every card, and the
    # alternative is fifty queries per page; it is a handful of narrow rows per
    # job, capped (see `services.score_history`).
    scores: Mapped[list["JobScore"]] = relationship(  # noqa: F821
        "JobScore", cascade="all, delete-orphan", lazy="selectin",
        order_by="desc(JobScore.created_at)", passive_deletes=True,
    )

    @property
    def score_history(self) -> list:
        """
        The evaluations worth showing: only when there is more than one.

        A job scored once has a history that says exactly what the card
        already says, and putting a disclosure widget on every card for that
        would be noise on the overwhelming majority of them.
        """
        return self.scores if len(self.scores or []) > 1 else []

    @property
    def effective_score(self) -> float | None:
        """
        The score that decided this job's fate, and the one to show.

        The deep pass ran because the first answer was a close call, so where
        there is one it is the better number — and displaying the first would
        contradict the decision that was actually made.
        """
        return self.llm_score_deep if self.llm_score_deep is not None else self.llm_score

    @property
    def salary_label(self) -> str | None:
        """
        The stated pay as one readable string, or None if it wasn't stated.

        A property rather than a template expression because three places show
        it and every one of them would otherwise reimplement "one bound, both
        bounds, or nothing" slightly differently.
        """
        low, high = self.salary_min, self.salary_max
        if low is None and high is None:
            return None
        symbol = {"USD": "$", "EUR": "€", "GBP": "£"}.get(
            (self.salary_currency or "USD").upper(), ""
        )
        suffix = "" if symbol else f" {self.salary_currency}" if self.salary_currency else ""

        def _short(amount: float) -> str:
            # Hourly and annual figures land in the same column; only the big
            # ones read better abbreviated.
            if amount >= 1000:
                return f"{symbol}{amount / 1000:g}k"
            return f"{symbol}{amount:g}"

        if low is not None and high is not None and low != high:
            return f"{_short(low)}–{_short(high)}{suffix}"
        return f"{_short(low if low is not None else high)}{suffix}"

    @property
    def employment_type_label(self) -> str | None:
        if not self.employment_type:
            return None
        return self.employment_type.replace("_", "-").title()

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
