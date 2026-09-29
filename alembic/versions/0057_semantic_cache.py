"""Bounded opt-in embedding cache; no vector extension or corpus-wide build."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0057"
down_revision = "0056"
branch_labels = depends_on = None


def upgrade():
    op.create_table("semantic_vectors",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("vector", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))


def downgrade():
    op.drop_table("semantic_vectors")
