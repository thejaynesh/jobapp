"""Move long-dead jobs out of the hot table, without forgetting them.

`jobs` is mostly descriptions, and most of those belong to postings the pipeline
rejected months ago. Nothing reads them again — a job filtered on a title
mismatch in June is not going to be reconsidered — and the text costs a
paragraph apiece across a hundred and fifty thousand rows.

What is not disposable is the fact that we have seen it. Deduplication has
three layers (the URL, the source's own id, the content hash) and deleting a
job defeats all three silently: the next fetch re-inserts the same posting as
new, spends a scoring call, reaches the same verdict, and does it again the
week after. So this keeps exactly those columns and nothing else, and
`deduplication.was_archived` reads it before anything is inserted.

Revision ID: 0028
Revises: 0027
"""
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0028"
down_revision: Union[str, None] = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "archived_jobs",
        # The job's original id, carried over rather than regenerated, so
        # anything still holding a reference can be traced to this row.
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True),
                  primary_key=True),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("source_job_id", sa.String(), nullable=True),
        sa.Column("source_urls", sa.dialects.postgresql.ARRAY(sa.String()),
                  nullable=True),
        sa.Column("url", sa.String(), nullable=False),
        # Unique for the same reason it is unique on `jobs`: two rows claiming
        # the same posting make "have we seen this?" ambiguous.
        sa.Column("dedupe_hash", sa.String(), nullable=False, unique=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("company", sa.String(), nullable=False),
        sa.Column("location", sa.String(), nullable=True),
        sa.Column("filter_reason", sa.String(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    # The three dedupe layers, in the order they are checked.
    op.create_index("ix_archived_jobs_url", "archived_jobs", ["url"])
    op.create_index("ix_archived_jobs_source_job", "archived_jobs",
                    ["source", "source_job_id"])
    op.create_index("ix_archived_jobs_filter_reason", "archived_jobs",
                    ["filter_reason"])
    op.create_index("ix_archived_jobs_archived_at", "archived_jobs",
                    [sa.text("archived_at DESC")])
    # GIN, because the URL layer asks "is this URL in the array" — which a
    # btree cannot answer and which runs once per fetched posting.
    op.create_index("ix_archived_jobs_source_urls", "archived_jobs",
                    ["source_urls"], postgresql_using="gin")


def downgrade() -> None:
    op.drop_index("ix_archived_jobs_source_urls", table_name="archived_jobs")
    op.drop_index("ix_archived_jobs_archived_at", table_name="archived_jobs")
    op.drop_index("ix_archived_jobs_filter_reason", table_name="archived_jobs")
    op.drop_index("ix_archived_jobs_source_job", table_name="archived_jobs")
    op.drop_index("ix_archived_jobs_url", table_name="archived_jobs")
    op.drop_table("archived_jobs")
