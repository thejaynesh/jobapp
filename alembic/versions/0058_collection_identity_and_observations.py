"""Preserve exact postings, collection progress and authoritative revisions.

The title/company/location fingerprint is useful for candidate comparison, but
cannot be unique: different requisitions and later openings share it. Existing
job/application IDs are retained. Listing identities are backfilled lazily from
verified sightings; inventing them from historical hashes would repeat the bug.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0058"
down_revision = "0057"
branch_labels = depends_on = None


def upgrade():
    op.execute("SET LOCAL maintenance_work_mem = '512MB'")
    # Constraint names differ for create_all databases and migrated databases.
    inspector = sa.inspect(op.get_bind())
    for table in ("jobs", "archived_jobs"):
        for constraint in inspector.get_unique_constraints(table):
            if constraint["column_names"] == ["dedupe_hash"]:
                op.drop_constraint(constraint["name"], table, type_="unique")
    op.add_column("jobs", sa.Column("identity_key", sa.String(64), nullable=True))
    op.create_unique_constraint("uq_jobs_identity_key", "jobs", ["identity_key"])
    op.add_column("jobs", sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True))
    for table in ("fetch_runs", "fetch_source_runs"):
        op.add_column(table, sa.Column("dropped", sa.Integer, nullable=False, server_default="0"))
    for column in (
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consecutive_failures", sa.Integer, nullable=False, server_default="0"),
        sa.Column("consecutive_not_found", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_new_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("fetch_cursor", JSONB, nullable=True),
    ):
        op.add_column("company_boards", column)
    op.create_index("ix_company_boards_next_due_at", "company_boards", ["next_due_at"])
    op.create_table("source_listings",
        sa.Column("identity_key", sa.String(64), primary_key=True),
        sa.Column("job_id", UUID(as_uuid=True), sa.ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("source", sa.String, nullable=False),
        sa.Column("board", sa.String, nullable=False),
        sa.Column("external_id", sa.String, nullable=True),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("details_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("upstream_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=True),
        sa.Column("snapshot", JSONB, nullable=True))
    op.create_index("ix_source_listings_job_id", "source_listings", ["job_id"])
    op.create_table("listing_revisions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("listing_key", sa.String(64), sa.ForeignKey("source_listings.identity_key", ondelete="CASCADE"), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("snapshot", JSONB, nullable=False))
    op.create_index("ix_listing_revisions_listing_key", "listing_revisions", ["listing_key"])
    op.create_table("fetch_board_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("run_id", UUID(as_uuid=True), sa.ForeignKey("fetch_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source", sa.String, nullable=False),
        sa.Column("board", sa.String, nullable=False),
        sa.Column("status", sa.String, nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_total", sa.Integer, nullable=True),
        sa.Column("returned", sa.Integer, nullable=False),
        sa.Column("inserted", sa.Integer, nullable=False),
        sa.Column("merged", sa.Integer, nullable=False),
        sa.Column("dropped", sa.Integer, nullable=False),
        sa.Column("cursor", JSONB, nullable=True),
        sa.Column("error_category", sa.String, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("payload", JSONB, nullable=True))
    op.create_index("ix_fetch_board_runs_run_id", "fetch_board_runs", ["run_id"])


def downgrade():
    # Refuse to discard valid distinct requisitions to satisfy the old schema.
    for table in ("jobs", "archived_jobs"):
        if op.get_bind().execute(sa.text(
                f"SELECT 1 FROM {table} GROUP BY dedupe_hash HAVING count(*) > 1 LIMIT 1")).first():
            raise RuntimeError("Collection downgrade would merge distinct postings; restore the prior backup instead")
    for table in ("fetch_board_runs", "listing_revisions", "source_listings"):
        op.drop_table(table)
    op.drop_index("ix_company_boards_next_due_at", table_name="company_boards")
    for column in ("last_success_at", "next_due_at", "consecutive_failures", "consecutive_not_found", "last_new_count", "fetch_cursor"):
        op.drop_column("company_boards", column)
    for table in ("fetch_runs", "fetch_source_runs"):
        op.drop_column(table, "dropped")
    op.drop_constraint("uq_jobs_identity_key", "jobs", type_="unique")
    op.drop_column("jobs", "identity_key")
    op.drop_column("jobs", "last_seen_at")
    for table in ("jobs", "archived_jobs"):
        op.create_unique_constraint(f"{table}_dedupe_hash_key", table, ["dedupe_hash"])
