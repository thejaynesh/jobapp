"""Why and when a job was dismissed from the jobs list.

"Not interested" set `filter_reason = "manual"` and nothing else, so a dozen
dismissals of senior roles and a dozen of on-site ones looked the same, and
nothing could say which setting would have kept them out or learn from them.
`dismiss_reason` is the reason the user picked (a key from
`match_report.DISMISS_REASONS`, or NULL when none was given); `dismissed_at`
is when, so the matching report can speak about the last week.

Two nullable columns with no default: in Postgres that is a catalogue change,
not a rewrite of `jobs`, so no index and no maintenance_work_mem needed.

Revision ID: 0048
Revises: 0047
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0048"
down_revision: Union[str, None] = "0047"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("dismiss_reason", sa.String(), nullable=True))
    op.add_column("jobs", sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "dismissed_at")
    op.drop_column("jobs", "dismiss_reason")
