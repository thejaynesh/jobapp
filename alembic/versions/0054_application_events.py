"""Preserve application milestones independently of the current board status.

The timeline index applies only to the new, initially empty event table. Historical
milestones are not invented: old applications retain their known current status.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "0054"
down_revision = "0053"
branch_labels = depends_on = None


def upgrade():
    op.create_table("application_events",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("application_id", pg.UUID(as_uuid=True), sa.ForeignKey("applications.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("origin", sa.String(40), nullable=False),
        sa.Column("dedupe_key", sa.String(160), nullable=False, unique=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("payload", pg.JSONB, nullable=False))
    op.create_index("ix_application_events_timeline", "application_events", ["application_id", "occurred_at"])


def downgrade():
    op.drop_table("application_events")
