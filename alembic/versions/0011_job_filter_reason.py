"""Record why each job was filtered out.

Revision ID: 0011
Revises: 0010
"""
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("filter_reason", sa.String(), nullable=True))
    op.add_column("jobs", sa.Column("filter_detail", sa.Text(), nullable=True))
    # The jobs list groups and filters by reason.
    op.create_index("ix_jobs_filter_reason", "jobs", ["filter_reason"])


def downgrade() -> None:
    op.drop_index("ix_jobs_filter_reason", table_name="jobs")
    op.drop_column("jobs", "filter_detail")
    op.drop_column("jobs", "filter_reason")
