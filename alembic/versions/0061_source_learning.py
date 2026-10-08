"""Keep diverse source evidence and retry learning independently per endpoint.

An unread response must not be discarded because a different endpoint on the
same host already has a reader. Existing readers remain host-wide fallbacks.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0061"
down_revision = "0060"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("crawl_recipes", sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("harvest_samples", sa.Column("page_url", sa.String(1000), nullable=True))
    op.add_column("harvest_samples", sa.Column("endpoint_key", sa.String(300), nullable=False, server_default=""))
    op.add_column("harvest_samples", sa.Column("fingerprint", sa.String(64), nullable=True))
    op.add_column("harvest_samples", sa.Column("shape_hash", sa.String(64), nullable=True))
    op.add_column("harvest_samples", sa.Column("observations", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("harvest_samples", sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("harvest_recipes", sa.Column("endpoint_key", sa.String(300), nullable=False, server_default=""))
    op.drop_index("uq_harvest_recipes_active", table_name="harvest_recipes")
    op.create_index("uq_harvest_recipes_active", "harvest_recipes", ["host", "endpoint_key"], unique=True,
                    postgresql_where=sa.text("status = 'active'"))
    op.create_table("harvest_learning_states",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("host", sa.String(160), nullable=False),
        sa.Column("endpoint_key", sa.String(300), nullable=False, server_default=""),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("evidence_hash", sa.String(64), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("claim_token", sa.String(36), nullable=True),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("result", postgresql.JSONB(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("host", "endpoint_key", name="uq_harvest_learning_endpoint"))


def downgrade():
    op.drop_column("crawl_recipes", "consecutive_failures")
    op.drop_table("harvest_learning_states")
    # Retain the newest active recipe when returning to one reader per host.
    op.execute("UPDATE harvest_recipes SET status = 'rejected' WHERE id IN "
               "(SELECT id FROM (SELECT id, row_number() OVER (PARTITION BY host ORDER BY created_at DESC, id) AS n "
               "FROM harvest_recipes WHERE status = 'active') ranked WHERE n > 1)")
    op.drop_index("uq_harvest_recipes_active", table_name="harvest_recipes")
    op.create_index("uq_harvest_recipes_active", "harvest_recipes", ["host"], unique=True,
                    postgresql_where=sa.text("status = 'active'"))
    op.drop_column("harvest_recipes", "endpoint_key")
    for name in ("last_seen_at", "observations", "shape_hash", "fingerprint", "endpoint_key", "page_url"):
        op.drop_column("harvest_samples", name)
