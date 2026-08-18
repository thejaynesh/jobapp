"""Which slice of the pipeline a fetch run was.

One 47-minute task fetched everything, so a cheap API source that could refresh
every hour waited on a browser tier that only needs to run twice a day, and
postings arrived hours later than they could have. The cycle is now three
tasks — API sources, ATS boards, and the browser tier — each on its own
schedule and its own lock.

`group` says which one a row is. Existing rows are stamped "all", which is
exactly what they were.

Revision ID: 0024
Revises: 0023
"""
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: Union[str, None] = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "fetch_runs",
        sa.Column("group", sa.String(), nullable=False, server_default="all"),
    )
    # The runs page filters and groups on this.
    op.create_index("ix_fetch_runs_group", "fetch_runs", ["group"])


def downgrade() -> None:
    op.drop_index("ix_fetch_runs_group", table_name="fetch_runs")
    op.drop_column("fetch_runs", "group")
