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
the index is capable; the statistics are what decide. This once raised the
column's statistics target to 1000 to help with that; measurement showed it
changed the estimate and not the plan, at six times the ANALYZE cost, so it is
gone — see the note by `ANALYZE` below for the numbers and why the column's
all-unique elements make a bigger sample pointless. The two lookups ahead of
it (`url`, `apply_url`) are now indexed and answer the common case, which is
the part that was worth fixing.

**CONCURRENTLY is deliberately not used**, and that needs stating carefully
because the original reasoning here was incomplete. It cannot run inside a
transaction and every other migration is transactional, which is true. It then
claimed "the lock is seconds, and the deploy applies migrations before any
worker serves traffic", and that second half was wrong in a way that caused an
outage: the deploy migrates before `up -d`, but the *old* containers are still
running and writing throughout. On the first real deployment this `CREATE
INDEX` queued behind an 11.5-hour-old write transaction from the old
fetch cycle, and because Postgres lock queues are FIFO, every later writer
queued behind it in turn. The table was unusable for an hour.

Two things fixed that, neither of them in this file: `job_fetcher` now commits
every 250 inserts instead of holding a cycle open (so there is no 11-hour
transaction to queue behind), and the deploy stops the workers before
migrating. If a future migration needs an index on a table this hot without
that second guarantee, take it out of the transaction and use CONCURRENTLY.

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
    # Room to sort the index in memory instead of on disk.
    #
    # At the 64MB default a GIN build over this table spills its sort, and the
    # spill is not small: measured on a 2-core VPS, this migration wrote 10GB
    # and took over an hour. `SET LOCAL` scopes it to this migration's
    # transaction, so nothing else on the server inherits it.
    #
    # 512MB rather than more, because this has to be safe on the smallest box
    # anyone runs it on, and it is per maintenance operation.
    op.execute("SET LOCAL maintenance_work_mem = '512MB'")

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
    # The new indexes need statistics before the planner will trust them.
    #
    # At the default target, not a raised one. An earlier version of this
    # migration set `SET STATISTICS 1000` on `source_urls`, reasoning that
    # element frequencies would stop the planner guessing. Measured, that was
    # wrong in the way that matters:
    #
    #     target 1000   ANALYZE 3439 ms   Bitmap Index Scan   est. 1 row
    #     target  100   ANALYZE  558 ms   Bitmap Index Scan   est. 600 rows
    #
    # The plan is the same either way — which is the only thing this migration
    # set out to achieve — and ANALYZE is six times cheaper. The reason is
    # structural: every element of `source_urls` is unique (120,000 elements,
    # 120,000 distinct), so there are no most-common elements to record and a
    # bigger sample learns nothing. The worse row estimate costs nothing here
    # because `find_existing_job` does single-table `.first()` lookups with no
    # join for it to mis-plan.
    op.execute("ANALYZE jobs")


def downgrade() -> None:
    op.drop_index("ix_jobs_apply_url", table_name="jobs")
    op.drop_index("ix_jobs_url", table_name="jobs")
    op.drop_index("ix_jobs_source_job", table_name="jobs")
    op.drop_index("ix_jobs_source_urls", table_name="jobs")
