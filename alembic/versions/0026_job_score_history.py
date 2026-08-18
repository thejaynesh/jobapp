"""Keep the verdict a job used to have.

Matching writes its answer into the job row, so every re-evaluation destroys
the one before it. That was fine while a job was scored once and never again.
It stopped being fine when enrichment started sending jobs back to be re-scored
the moment their description grew: a job rejected at 45 on a 500-character stub
and then accepted at 82 on the real posting shows only the 82, and the evidence
that the pipeline changed its mind is exactly the part worth reading.

One row per evaluation, appended before the job row is overwritten. Pruned per
job rather than per table, so a job's first verdict survives however many
thousand calls the pipeline has made since.

Revision ID: 0026
Revises: 0025
"""
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: Union[str, None] = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job_scores",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True),
                  primary_key=True),
        sa.Column("job_id", sa.dialects.postgresql.UUID(as_uuid=True),
                  nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("llm_score", sa.Float(), nullable=True),
        sa.Column("llm_score_deep", sa.Float(), nullable=True),
        sa.Column("matched_by", sa.String(), nullable=True),
        sa.Column("deep_matched_by", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default=""),
        sa.Column("filter_reason", sa.String(), nullable=True),
        sa.Column("filter_detail", sa.Text(), nullable=True),
        sa.Column("reasoning", sa.Text(), nullable=True),
        sa.Column("min_score", sa.Float(), nullable=True),
        sa.Column("description_chars", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.Column("trigger", sa.String(), nullable=False,
                  server_default="initial"),
        # Cascade on purpose: a row explaining a job that no longer exists has
        # nothing left to explain.
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_job_scores_job_id", "job_scores", ["job_id"])
    # The one query this table serves: a job's evaluations, newest first.
    op.create_index(
        "ix_job_scores_job_created", "job_scores",
        ["job_id", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_job_scores_job_created", table_name="job_scores")
    op.drop_index("ix_job_scores_job_id", table_name="job_scores")
    op.drop_table("job_scores")
