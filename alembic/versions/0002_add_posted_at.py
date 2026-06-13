"""add posted_at to jobs

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-13 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "posted_at")
