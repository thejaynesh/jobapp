"""Durable ingestion state, separate from acknowledging a browser result.

Existing rows retain their prior completed interpretation. New completions set
pending explicitly. No index or full-table backfill is needed for this change.
"""
from alembic import op
import sqlalchemy as sa

revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("browser_tasks", sa.Column("ingestion_status", sa.String(), nullable=False, server_default="done"))
    op.add_column("browser_tasks", sa.Column("ingestion_error", sa.Text(), nullable=True))
    op.add_column("browser_tasks", sa.Column("ingestion_attempts", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("browser_tasks", sa.Column("ingestion_retry_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    for column in ("ingestion_retry_at", "ingestion_attempts", "ingestion_error", "ingestion_status"):
        op.drop_column("browser_tasks", column)
