"""Published accounts of interviewing at a company.

Storing them is easy; deciding which of forty is worth reading is the value, so
the indexes are shaped for "this company, newest first" and posted_at is NOT
NULL — a report that cannot be placed on a recency scale cannot be ranked, and
loops change often enough that an undated one is closer to noise than to data.

Revision ID: 0017
Revises: 0016
"""
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "interview_reports",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("company_key", sa.String(), nullable=False),
        sa.Column("company", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False, server_default=""),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("role_hint", sa.String(), nullable=True),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("url", name="uq_interview_reports_url"),
    )
    op.create_index(
        "ix_interview_reports_company_recency",
        "interview_reports",
        ["company_key", "posted_at"],
    )
    op.create_index("ix_interview_reports_source", "interview_reports", ["source"])


def downgrade() -> None:
    op.drop_index("ix_interview_reports_source", table_name="interview_reports")
    op.drop_index("ix_interview_reports_company_recency", table_name="interview_reports")
    op.drop_table("interview_reports")
