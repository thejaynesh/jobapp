"""Decision-time feature snapshots and impressions for honest ranking evaluation."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "0055"
down_revision = "0054"
branch_labels = depends_on = None


def upgrade():
    op.create_table("decision_events",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("job_id", pg.UUID(as_uuid=True), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("dedupe_key", sa.String(160), nullable=False, unique=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", pg.JSONB, nullable=False))
    op.create_index("ix_decision_events_job_time", "decision_events", ["job_id", "occurred_at"])
    op.create_index("ix_decision_events_time", "decision_events", ["occurred_at"])


def downgrade():
    op.drop_table("decision_events")
