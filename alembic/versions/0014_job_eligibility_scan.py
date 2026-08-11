"""What the posting said about sponsorship, quoted and kept beside the job.

Advisory only. The citizens-only half of the eligibility scan reuses the
existing `filter_reason` / `filter_detail` columns because it behaves like every
other filter; this half behaves like nothing else in the schema — it is shown
and never acted on — so it gets its own home rather than borrowing one that
implies filtering.

Revision ID: 0014
Revises: 0013
"""
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("sponsorship_note", sa.Text(), nullable=True))
    op.add_column("jobs", sa.Column("sponsorship_direction", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "sponsorship_direction")
    op.drop_column("jobs", "sponsorship_note")
