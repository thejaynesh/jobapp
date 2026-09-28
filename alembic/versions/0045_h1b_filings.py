"""Employers' H-1B filings per fiscal quarter: `h1b_filings`.

Counted from the Department of Labor's public LCA disclosure files by
`services.sponsorship_history`, so a job can say whether its employer has
sponsored H-1B workers lately, and how many for computer occupations —
which is what an F-1 graduate needs to know and what a posting rarely says.

About 25,000 employers a quarter, and the window is a few quarters, so the
table stays near 100,000 short rows. The primary key leads with the quarter,
which is the only filter anything applies (`WHERE quarter IN (…)`); lookups
by employer are made against an in-memory copy of the window's totals, so
there is no second index to maintain.

A new table, so nothing waits on a lock.

Revision ID: 0045
Revises: 0044
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0045"
down_revision: Union[str, None] = "0044"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "h1b_filings",
        sa.Column("quarter", sa.String(length=12), nullable=False),
        sa.Column("employer_key", sa.String(), nullable=False),
        sa.Column("employer_name", sa.String(), nullable=False),
        sa.Column("certified", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("computer", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("new_employment", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("quarter", "employer_key"),
    )


def downgrade() -> None:
    op.drop_table("h1b_filings")
