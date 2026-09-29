"""Versioned assessment alongside the existing scores; no full-table backfill."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0056"
down_revision = "0055"
branch_labels = depends_on = None


def upgrade():
    op.add_column("jobs", sa.Column("match_assessment", JSONB, nullable=True))


def downgrade():
    op.drop_column("jobs", "match_assessment")
