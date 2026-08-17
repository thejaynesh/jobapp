"""Salary, required years, skills and the rest, as columns.

"Skills and any other details" as first-class data rather than prose. Today
salary, required experience and employment type live buried in the description:
the matcher re-derives them from text on every scoring call, the UI cannot show
them, and nothing can filter on them. One LLM call per job that passes the
keyword filter fills these in, and the matcher then reads facts instead of
guessing at paragraphs.

Every column is nullable, and stays null when the posting does not say. A
guessed salary is worse than a missing one — it would filter out jobs on a
number nobody wrote down.

Revision ID: 0022
Revises: 0021
"""
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022"
down_revision: Union[str, None] = "0021"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("salary_min", sa.Float()),
    ("salary_max", sa.Float()),
    ("salary_currency", sa.String()),
    ("employment_type", sa.String()),
    ("required_years", sa.Float()),
    ("required_skills", postgresql.ARRAY(sa.String())),
    ("nice_to_have_skills", postgresql.ARRAY(sa.String())),
    ("education_required", sa.String()),
    ("benefits_note", sa.Text()),
    ("language", sa.String()),
    # When the details were last read out of the description, so a description
    # that grows can trigger a re-read and one that hasn't doesn't pay for a
    # second call.
    ("details_extracted_at", sa.DateTime(timezone=True)),
)


def upgrade() -> None:
    for name, type_ in _COLUMNS:
        op.add_column("jobs", sa.Column(name, type_, nullable=True))
    # The jobs page filters on a salary floor, and non-English postings are
    # skipped by language — both over the whole table.
    op.create_index("ix_jobs_salary_min", "jobs", ["salary_min"])
    op.create_index("ix_jobs_language", "jobs", ["language"])


def downgrade() -> None:
    op.drop_index("ix_jobs_language", table_name="jobs")
    op.drop_index("ix_jobs_salary_min", table_name="jobs")
    for name, _ in reversed(_COLUMNS):
        op.drop_column("jobs", name)
