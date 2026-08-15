"""When a document generation started running.

Without it, `generation_status = 'generating'` is a state with no clock on it,
and there is no way to tell a generation that is thirty seconds in from one
whose worker was killed three days ago. Both read as "in progress" forever,
which is precisely the silence this column exists to break: the sweeper uses it
to decide what to re-queue.

Revision ID: 0018
Revises: 0017
"""
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "applications",
        sa.Column("generation_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Anything already sitting at 'generating' has no start time and never
    # will, so it would be invisible to the sweeper forever. Stamping it now
    # makes it eligible one interval from deploy rather than never.
    op.execute(
        "UPDATE applications SET generation_started_at = now() "
        "WHERE generation_status = 'generating'"
    )


def downgrade() -> None:
    op.drop_column("applications", "generation_started_at")
