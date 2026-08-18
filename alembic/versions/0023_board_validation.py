"""Prove a discovered board exists before polling it forever.

The registry holds gems next to junk: an auto-discovered `ionq` yielding 5,460
jobs sits beside `greenhouse/linkedin`, `greenhouse/appcast` and
`greenhouse/stepstone` — slugs "discovered" from pages that were not a company
board at all, some attached to the wrong company. Nothing ever checked, so each
of them was polled every cycle for months and counted against the per-ATS
budget that real companies were competing for.

`validated_at` records that a board was probed; `inactive_reason` says what the
probe found when it failed. Existing rows are stamped as validated from their
first sighting: they have been polled for months and their yield history is
better evidence than a fresh probe would be.

Revision ID: 0023
Revises: 0022
"""
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: Union[str, None] = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "company_boards",
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "company_boards", sa.Column("inactive_reason", sa.String(), nullable=True)
    )
    # Everything already in the registry has been polled for months; its yield
    # history is stronger evidence than a probe, and treating it all as
    # unvalidated would stop the whole registry dead until the backlog drained.
    op.execute("UPDATE company_boards SET validated_at = first_seen_at")
    # The validation pass selects on exactly this.
    op.create_index(
        "ix_company_boards_validated_at", "company_boards", ["validated_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_company_boards_validated_at", table_name="company_boards")
    op.drop_column("company_boards", "inactive_reason")
    op.drop_column("company_boards", "validated_at")
