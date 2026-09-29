"""Persist delivery claims so concurrent and interrupted sends cannot repeat silently."""
from alembic import op
import sqlalchemy as sa

revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("outreach_messages", sa.Column("send_state", sa.String(), nullable=False, server_default="idle"))
    op.add_column("outreach_messages", sa.Column("send_started_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    op.drop_column("outreach_messages", "send_started_at")
    op.drop_column("outreach_messages", "send_state")
