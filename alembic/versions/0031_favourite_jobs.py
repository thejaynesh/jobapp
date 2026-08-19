"""Jobs the user picked out to apply to.

Between "the matcher scored this 82" and "I have written documents for it"
there was nothing — no way to say *I want this one* and find it again. The
matched list is hundreds of rows deep and re-sorts itself every time a pass
runs, so a job noticed on Tuesday was genuinely hard to get back to on Friday.

Its own column rather than a status: a favourite is orthogonal to where the job
sits in the pipeline. Starring one does not un-filter it, and filtering one does
not un-star it — the two answer different questions and folding them together
would lose whichever was set second.

`favourited_at` is what the favourites view sorts by. Newest first is the useful
order there, and `fetched_at` would put a job starred this morning below one
starred last month.

Revision ID: 0031
Revises: 0030
"""
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0031"
down_revision: Union[str, None] = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column("favourite", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
    )
    op.add_column(
        "jobs",
        sa.Column("favourited_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Partial: favourites are a handful of rows in a table of hundreds of
    # thousands, and the only query is "the starred ones". Indexing the false
    # side would be indexing the whole table to find nothing.
    op.create_index(
        "ix_jobs_favourite", "jobs", ["favourited_at"],
        postgresql_where=sa.text("favourite"),
    )


def downgrade() -> None:
    op.drop_index("ix_jobs_favourite", table_name="jobs")
    op.drop_column("jobs", "favourited_at")
    op.drop_column("jobs", "favourite")
