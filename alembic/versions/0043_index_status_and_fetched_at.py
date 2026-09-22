"""Index `(status, fetched_at)` so the age-ordered reads stop sorting.

Composite, and in that order, because a single-column index on `fetched_at`
was measured and **does not get used**. That was the first version of this
migration, justified by four call sites that filter or order by the column.
With a realistic spread — 120,000 rows over 120 days, 83% of them older than
the dashboard's window — the planner ignored it in every one:

    archive.candidates          Sort -> Index Scan using ix_jobs_status
    enrichment.select_targets   Sort -> Seq Scan
    the dashboard age window            Index Scan using ix_jobs_status

Each of those queries leads with a `status` predicate, and `ix_jobs_status`
has existed since 0003, so Postgres takes the selective index and sorts or
filters afterwards. A second index on `fetched_at` alone gives it nothing it
wants.

`(status, fetched_at)` is a different proposition: the status predicate uses
the leading column and `fetched_at` then supplies the ordering, so the sort
node disappears rather than being fed by an index scan. Measured on
`archive.candidates`' exact shape — `status = 'filtered_out' AND fetched_at <
cutoff ORDER BY fetched_at LIMIT 5000`:

    with    ix_jobs_status_fetched     34.9 ms   no sort
    without                           110.2 ms   top-N heapsort, 979kB

Three times faster, and the gap widens with the table: the sort is over every
settled rejection, a population the review measured at 58,000 rows and
growing, while the index scan stops after the limit.

One index, not two, and it replaces rather than supplements. Migration 0042
immediately before this one lowers the autovacuum threshold precisely because
every index makes every vacuum more expensive, so adding one that no query
chooses would have been the worst of both.

`maintenance_work_mem` for the reason 0038 sets it: the default spills the
build's sort, and on two cores that was measured in hours rather than minutes.

Revision ID: 0043
Revises: 0042
"""
from typing import Union

from alembic import op

revision: str = "0043"
down_revision: Union[str, None] = "0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL maintenance_work_mem = '512MB'")
    op.create_index("ix_jobs_status_fetched", "jobs", ["status", "fetched_at"])


def downgrade() -> None:
    op.drop_index("ix_jobs_status_fetched", table_name="jobs")
