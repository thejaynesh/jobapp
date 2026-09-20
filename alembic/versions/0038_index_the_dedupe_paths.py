"""Index the three questions asked most often about a job.

`jobs` had twelve indexes and none of them covered the queries that run on
every fetched posting and every overlay render.

`deduplication.find_existing_job` asks three questions per posting, in order:
is this URL already in `source_urls`, is this `(source, source_job_id)` already
ours, is this content hash already ours. Only the third was indexed — it is a
UNIQUE column. Measured against 120,000 rows, on a miss, which is the case a
fetch is *for*:

    layer 1   url = ANY(source_urls)          49.6 ms, 120,000 rows scanned
    layer 2   source = ? AND source_job_id = ?  32.5 ms, 120,000 rows scanned
    layer 3   dedupe_hash = ?                  ~0.05 ms

`ix_jobs_source` exists but `source` has about twenty distinct values, so the
planner correctly declines it and scans. At the ~300,000 rows this table
reaches, a new posting was paying roughly 200 ms before it could be written.

`job_context.find_job` — the extension overlay, once per job page the user
opens — asks a fourth: `url`, then `apply_url`, then `source_urls` again. Three
more unindexed scans, on the one latency a person actually waits for.

Two notes for whoever reads this next.

**The GIN index only works with the right operator.** Migration 0028 added
exactly this index on `archived_jobs.source_urls`, with a comment explaining
why it was needed — and it has never once been used, because `was_archived`
asks with `.any()`, which SQLAlchemy emits as `= ANY(...)`, and GIN cannot
answer that. Only `@>` (contains) and `&&` (overlap) reach it. Both call sites
were changed to `.contains([url])` in the same commit as this migration; the
index and the query shape are one change and neither works alone:

    source_urls @> ARRAY['…']::varchar[]    0.065 ms   Bitmap Index Scan
    '…' = ANY(source_urls)                 48.283 ms   Seq Scan

`find_job`'s `.overlap()` already emits `&&`, which GIN can also answer, so it
needs no code change — with one honest caveat. Whether the planner *chooses*
the index for `&&` depends on its selectivity estimate for the array elements,
and under `LIMIT 1` a bad estimate makes a sequential scan look like the
cheaper gamble. Forced (`enable_seqscan=off`) it is 0.096 ms against 67 ms, so
the index is capable; the statistics are what decide. Hence the raised
statistics target below — a hint that lets ANALYZE collect element frequencies
rather than fall back to a default guess. It is not a guarantee, and the two
lookups ahead of it (`url`, `apply_url`) are now indexed and answer the common
case, which is the part that was worth fixing.

**CONCURRENTLY is deliberately not used.** It cannot run inside a transaction,
and every other migration here is transactional; on a table this size the lock
is seconds, and the deploy applies migrations before any worker serves traffic.

Revision ID: 0038
Revises: 0037
"""
from typing import Union

from alembic import op

revision: str = "0038"
down_revision: Union[str, None] = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Dedupe layer 1, and the overlay's last resort. GIN because the question
    # is "is this value in the array", which a btree cannot answer.
    op.create_index(
        "ix_jobs_source_urls", "jobs", ["source_urls"], postgresql_using="gin"
    )
    # Dedupe layer 2. Composite, because `source` alone is twenty values.
    op.create_index("ix_jobs_source_job", "jobs", ["source", "source_job_id"])
    # The overlay's first two questions.
    op.create_index("ix_jobs_url", "jobs", ["url"])
    op.create_index("ix_jobs_apply_url", "jobs", ["apply_url"])
    # Give ANALYZE something to go on for the array. Without element
    # frequencies the planner guesses, and a guess under LIMIT 1 is what makes
    # it prefer a sequential scan to the index above. Takes effect on the next
    # ANALYZE (autovacuum's, or the one below).
    op.execute("ALTER TABLE jobs ALTER COLUMN source_urls SET STATISTICS 1000")
    op.execute("ANALYZE jobs")


def downgrade() -> None:
    op.execute("ALTER TABLE jobs ALTER COLUMN source_urls SET STATISTICS -1")
    op.drop_index("ix_jobs_apply_url", table_name="jobs")
    op.drop_index("ix_jobs_url", table_name="jobs")
    op.drop_index("ix_jobs_source_job", table_name="jobs")
    op.drop_index("ix_jobs_source_urls", table_name="jobs")
