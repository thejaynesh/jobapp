"""Every source that has listed a job: `jobs.seen_by`.

`jobs.source` names the source that stored a posting first, and every later
sighting only added a URL to `source_urls`. So nothing could say which jobs a
source found that no other source did, or how many of SimplifyJobs' postings
our own board readers also reached — the two numbers that say whether a
source earns its requests (`services.source_yield`).

`["simplify", "lever"]`: appended to on each sighting by a source not yet in
it. NULL on rows stored before this, and read as `[source]`, which is what
those rows are known to have been seen by; they fill in as they are listed
again. No backfill, so the migration rewrites nothing.

No index: it is read by one daily aggregate over the recent window, which
filters on `fetched_at` and `status` (0043's index), and every index makes
every vacuum dearer (0042).

Adding a nullable column with no default is a catalogue change in Postgres,
not a table rewrite; the deploy stops the workers before migrating anyway.

Revision ID: 0046
Revises: 0045
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0046"
down_revision: Union[str, None] = "0045"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("seen_by", postgresql.ARRAY(sa.String()), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "seen_by")
