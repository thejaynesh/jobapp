"""What happens next on an application, and when.

The applications page listed statuses and nothing else, so "which of these
twenty applications is waiting on me?" had no answer but reading each one.
`next_action` is a short line ("Follow up if no reply"), `next_action_due`
the day it is due; both are set to a sensible default when the status
changes, and editable. `status_changed_at` is when the status last moved,
which the board shows as "N days in this column".

Three nullable columns on `applications`, a small table.

Revision ID: 0050
Revises: 0049
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0050"
down_revision: Union[str, None] = "0049"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("applications", sa.Column("next_action", sa.String(), nullable=True))
    op.add_column("applications", sa.Column("next_action_due", sa.Date(), nullable=True))
    op.add_column("applications",
                  sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("applications", "status_changed_at")
    op.drop_column("applications", "next_action_due")
    op.drop_column("applications", "next_action")
