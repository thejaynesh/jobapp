"""Shared employer evidence and explicit watchlists, preserving existing job/contact IDs.

Job linkage is populated by bounded application work rather than a bulk
name-based migration that could conflate employers. No jobs index is built.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0060"
down_revision = "0059"
branch_labels = depends_on = None


def upgrade():
    op.create_table("companies",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("domain", sa.String(253), unique=True),
        sa.Column("domain_verified_at", sa.DateTime(timezone=True)),
        sa.Column("identity_source", sa.String(30), nullable=False, server_default="job"),
        sa.Column("aliases", JSONB, nullable=False, server_default="[]"),
        sa.Column("evidence", JSONB, nullable=False, server_default="[]"),
        sa.Column("careers_url", sa.Text),
        sa.Column("watched", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("research_status", sa.String(30), nullable=False, server_default="pending"),
        sa.Column("research_note", sa.Text),
        sa.Column("last_researched_at", sa.DateTime(timezone=True)),
        sa.Column("next_refresh_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    for table in ("jobs", "contacts", "company_boards"):
        op.add_column(table, sa.Column("company_id", UUID(as_uuid=True), nullable=True))
        op.create_foreign_key(f"fk_{table}_company_id", table, "companies", ["company_id"], ["id"], ondelete="SET NULL")
    op.create_index("ix_contacts_company_id", "contacts", ["company_id"])
    op.create_index("ix_company_boards_company_id", "company_boards", ["company_id"])


def downgrade():
    op.drop_index("ix_company_boards_company_id", "company_boards")
    op.drop_index("ix_contacts_company_id", "contacts")
    for table in ("company_boards", "contacts", "jobs"):
        op.drop_constraint(f"fk_{table}_company_id", table, type_="foreignkey")
        op.drop_column(table, "company_id")
    op.drop_table("companies")
