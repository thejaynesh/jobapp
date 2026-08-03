"""Track which AI provider/model matched each job and generated each document.

Revision ID: 0008
Revises: 0007
"""
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("matched_by", sa.String(), nullable=True))
    op.add_column(
        "application_documents", sa.Column("generated_by", sa.String(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("application_documents", "generated_by")
    op.drop_column("jobs", "matched_by")
