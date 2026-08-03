"""Company ATS board registry, and the resolved apply URL on jobs.

Revision ID: 0009
Revises: 0008
"""
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("apply_url", sa.String(), nullable=True))

    op.create_table(
        "company_boards",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("ats", sa.String(), nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("company", sa.String(), nullable=True),
        sa.Column("origin", sa.String(), nullable=False, server_default="discovered"),
        sa.Column("source_host", sa.String(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("consecutive_empty", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_job_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_job_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("ats", "slug", name="uq_company_boards_ats_slug"),
    )
    op.create_index("ix_company_boards_ats", "company_boards", ["ats"])
    op.create_index("ix_company_boards_active", "company_boards", ["active"])
    # The per-cycle slug pick is "active boards for this ATS, best first".
    op.create_index(
        "ix_company_boards_ranking",
        "company_boards",
        ["ats", "active", "last_job_count", "last_seen_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_company_boards_ranking", table_name="company_boards")
    op.drop_index("ix_company_boards_active", table_name="company_boards")
    op.drop_index("ix_company_boards_ats", table_name="company_boards")
    op.drop_table("company_boards")
    op.drop_column("jobs", "apply_url")
