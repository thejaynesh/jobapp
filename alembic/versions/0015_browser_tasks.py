"""Work the server wants a browser to do.

The server cannot reach LinkedIn as the user — no residential IP, no logged-in
session. So it writes down what it needs here and whichever engine is awake on
the laptop picks it up. Two deadlines rather than one: `lease_expires_at`
returns work an agent abandoned, `expires_at` retires work that stopped being
worth doing.

Revision ID: 0015
Revises: 0014
"""
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "browser_tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("status", sa.String(), nullable=False, server_default="queued"),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("agent_id", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("leased_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    # The leasing query, which is the only hot path on this table.
    op.create_index(
        "ix_browser_tasks_claimable",
        "browser_tasks",
        ["status", "kind", "priority", "created_at"],
    )
    op.create_index(
        "ix_browser_tasks_lease_expires_at", "browser_tasks", ["lease_expires_at"]
    )
    op.create_index("ix_browser_tasks_expires_at", "browser_tasks", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_browser_tasks_expires_at", table_name="browser_tasks")
    op.drop_index("ix_browser_tasks_lease_expires_at", table_name="browser_tasks")
    op.drop_index("ix_browser_tasks_claimable", table_name="browser_tasks")
    op.drop_table("browser_tasks")
