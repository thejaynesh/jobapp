"""Per-cycle fetch history, so more than the latest run is inspectable.

Revision ID: 0010
Revises: 0009
"""
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fetch_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="ok"),
        sa.Column("fetched", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("inserted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("merged", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stale", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("queries", postgresql.ARRAY(sa.String()), nullable=True),
        sa.Column("locations", postgresql.ARRAY(sa.String()), nullable=True),
        sa.Column("links_attempted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("links_resolved", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("links_failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("boards_polled", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("boards_discovered", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("boards_sniffed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("backfill", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
    )
    # The only access pattern is "most recent N runs".
    op.create_index("ix_fetch_runs_started_at", "fetch_runs", ["started_at"])

    op.create_table(
        "fetch_source_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("status", sa.String(), nullable=False, server_default="ok"),
        sa.Column("fetched", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("inserted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("merged", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stale", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("errors", postgresql.ARRAY(sa.String()), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["fetch_runs.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_fetch_source_runs_run_id", "fetch_source_runs", ["run_id"])
    op.create_index(
        "ix_fetch_source_runs_source", "fetch_source_runs", ["source"]
    )


def downgrade() -> None:
    op.drop_index("ix_fetch_source_runs_source", table_name="fetch_source_runs")
    op.drop_index("ix_fetch_source_runs_run_id", table_name="fetch_source_runs")
    op.drop_table("fetch_source_runs")
    op.drop_index("ix_fetch_runs_started_at", table_name="fetch_runs")
    op.drop_table("fetch_runs")
