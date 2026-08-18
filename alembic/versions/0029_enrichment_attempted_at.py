"""Remember that enrichment already tried this job.

A pass that found nothing left no mark: `apply_extraction` returns early when
there is no better description, so the job keeps its thin one and nothing about
it changes. `select_targets` orders by `fetched_at DESC` and takes the newest
thin jobs — which means the same unenrichable postings are picked again by
every pass, forever, and the older backlog behind them is never reached.

A stamp rather than a flag, so a host that was refusing us in March is tried
again in April rather than written off permanently.

Revision ID: 0029
Revises: 0028
"""
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: Union[str, None] = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column("enrichment_attempted_at", sa.DateTime(timezone=True),
                  nullable=True),
    )
    # Read on every target selection, which runs once per pass over a table
    # where most rows are thin descriptions.
    op.create_index(
        "ix_jobs_enrichment_attempted_at", "jobs", ["enrichment_attempted_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_jobs_enrichment_attempted_at", table_name="jobs")
    op.drop_column("jobs", "enrichment_attempted_at")
