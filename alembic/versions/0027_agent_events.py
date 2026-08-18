"""What the browser extension did, kept where it can be counted.

Everything the extension does happens on someone else's page and leaves no
trace here. A harvest that found nothing, an autofill that recognised two
fields out of fifteen, an overlay lookup on a site the URL matcher cannot
resolve — all of them are silence, and silence is indistinguishable from an
extension that was uninstalled a week ago. Every question about this subsystem
has started with "is it even installed?" for months.

One row per event, shaped for aggregation: a closed `kind`, the host rather
than the URL (the URL is the posting, which is already in `jobs`, and a table
of URLs is a browsing history nobody needs kept), and a free-shaped JSONB
summary. Pruned on a timer like the LLM log.

Revision ID: 0027
Revises: 0026
"""
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: Union[str, None] = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_events",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True),
                  primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False, server_default="other"),
        sa.Column("host", sa.String(160), nullable=True),
        sa.Column("agent_id", sa.String(120), nullable=True),
        sa.Column("ok", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("summary", sa.dialects.postgresql.JSONB(), nullable=True),
    )
    op.create_index(
        "ix_agent_events_created", "agent_events",
        [sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_agent_events_kind_created", "agent_events",
        ["kind", sa.text("created_at DESC")],
    )
    # Finishing a task is what makes it prunable, and nothing has ever pruned
    # this table — so the index the sweeper needs did not exist either.
    op.create_index(
        "ix_browser_tasks_completed_at", "browser_tasks", ["completed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_browser_tasks_completed_at", table_name="browser_tasks")
    op.drop_index("ix_agent_events_kind_created", table_name="agent_events")
    op.drop_index("ix_agent_events_created", table_name="agent_events")
    op.drop_table("agent_events")
