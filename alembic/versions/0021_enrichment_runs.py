"""What each enrichment pass went and got.

Enrichment is the only step that goes back for what a source left out — Adzuna
truncates every description at 500 characters, LinkedIn ships 90% of its jobs
without one at all — and "is it working?" is a question with several parts:
which method won, how much text was gained, how many jobs got a second chance
at matching, and which hosts are refusing us. A row per run answers all of them
over time; a log line answers none of them.

Revision ID: 0021
Revises: 0020
"""
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021"
down_revision: Union[str, None] = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "enrichment_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="ok"),
        sa.Column("attempted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("enriched", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unchanged", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("via_ats_api", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("via_json_ld", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("via_llm", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("via_landing_html", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("queued_browser", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("chars_gained", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("requeued_for_matching", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.Column("failures_by_host", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_enrichment_runs_started_at", "enrichment_runs", ["started_at"]
    )
    # Enrichment picks its targets by "shortest description first, newest
    # first" over the whole table. Without this the selection query is a
    # sequential scan of 150k rows every few minutes.
    op.create_index(
        "ix_jobs_enrichment_targets",
        "jobs",
        ["fetched_at"],
        postgresql_where=sa.text("description IS NULL OR length(description) < 1500"),
    )


def downgrade() -> None:
    op.drop_index("ix_jobs_enrichment_targets", table_name="jobs")
    op.drop_index("ix_enrichment_runs_started_at", table_name="enrichment_runs")
    op.drop_table("enrichment_runs")
