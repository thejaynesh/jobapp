"""
What people wrote about interviewing somewhere.

Candidates publish remarkably detailed writeups — rounds, questions, timeline,
outcome. One is an anecdote; a few dozen for the same company, weighted by
recency, is a targeted prep plan and a ranking signal about which loops cost
four weeks before you apply.

The schema is shaped by the fact that **retrieval is the hard part, not
ingestion**. Storing text is easy. Deciding which of forty reports is worth
reading is where the value is, and three fields carry that decision:

  `company_key`  — fuzzy company identity, the same normalization the rest of
                   the app uses, so "Acme, Inc." and "Acme Inc" are one company.
  `posted_at`    — required, not optional. Loops change; a 2019 report is noise
                   next to one from last quarter, and a report with no date
                   cannot be placed on that scale at all. Undated reports are
                   refused at ingest rather than stored and discounted, because
                   a corpus full of unrankable rows is worse than a smaller one.
  `role_hint`    — "SDE-1 University Grad" and "SDE-2 lateral" are different
                   loops at the same company. Free text, because every company
                   names its levels differently and a taxonomy would be wrong.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# Where a report came from. Free sources first — these need no key, no login,
# and no extension. The walled ones (LeetCode Discuss, company tags, Glassdoor)
# arrive with the extension and slot in beside these without a schema change.
REPORT_SOURCES = ("geeksforgeeks", "reddit", "github")


class InterviewReport(Base):
    """One published account of interviewing at a company."""

    __tablename__ = "interview_reports"
    __table_args__ = (
        # The same writeup gets syndicated and re-fetched; the URL is its
        # identity.
        UniqueConstraint("url", name="uq_interview_reports_url"),
        # Retrieval is always "this company, newest first".
        Index("ix_interview_reports_company_recency", "company_key", "posted_at"),
        Index("ix_interview_reports_source", "source"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    # Normalized identity, and the name as the source wrote it. Both, because
    # the key is what matching needs and the original is what a person reads.
    company_key: Mapped[str] = mapped_column(String, nullable=False)
    company: Mapped[str] = mapped_column(String, nullable=False)

    source: Mapped[str] = mapped_column(String, nullable=False)
    url: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False, default="")
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # Required. See the module docstring: an undated report cannot be ranked,
    # and ranking is the whole point.
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # "SDE-1", "new grad", "summer intern" — as written, not normalized.
    role_hint: Mapped[str | None] = mapped_column(String, nullable=True)

    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<InterviewReport {self.company_key} {self.source} {self.posted_at:%Y-%m}>"
