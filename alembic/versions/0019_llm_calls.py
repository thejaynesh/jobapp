"""Every request to a language model, with what went in and what came back.

The existing log lines record that a call happened and how it ended, which is
the one thing you can already infer from the result. What was missing is the
pair — prompt and reply, stored together — because "the resume came out empty"
is either a bad prompt or a bad answer and nothing else can tell you which.

Indexed for the two ways it actually gets read: newest first, and everything
belonging to one application.

Revision ID: 0019
Revises: 0018
"""
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "llm_calls",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("stage", sa.String(40), nullable=False, server_default="unknown"),
        sa.Column("provider", sa.String(40), nullable=False, server_default=""),
        sa.Column("model", sa.String(160), nullable=False, server_default=""),
        sa.Column("messages", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'[]'::jsonb")),
        sa.Column("temperature", sa.Float(), nullable=True),
        sa.Column("max_tokens", sa.Integer(), nullable=True),
        sa.Column("response", sa.Text(), nullable=True),
        sa.Column("reasoning", sa.Text(), nullable=True),
        sa.Column("finish_reason", sa.String(40), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ok", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("error", sa.Text(), nullable=True),
        # Deliberately not foreign keys: the log has to outlive what it refers
        # to, and a cascade would delete the evidence with the job.
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("application_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index("ix_llm_calls_created_at", "llm_calls",
                    [sa.text("created_at DESC")])
    op.create_index("ix_llm_calls_stage_created", "llm_calls",
                    ["stage", sa.text("created_at DESC")])
    op.create_index("ix_llm_calls_application", "llm_calls", ["application_id"])
    op.create_index("ix_llm_calls_job", "llm_calls", ["job_id"])


def downgrade() -> None:
    op.drop_index("ix_llm_calls_job", table_name="llm_calls")
    op.drop_index("ix_llm_calls_application", table_name="llm_calls")
    op.drop_index("ix_llm_calls_stage_created", table_name="llm_calls")
    op.drop_index("ix_llm_calls_created_at", table_name="llm_calls")
    op.drop_table("llm_calls")
