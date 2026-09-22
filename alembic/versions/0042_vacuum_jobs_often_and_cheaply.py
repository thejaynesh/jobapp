"""Let autovacuum reach `jobs` before 70,000 rows are dead.

Measured on the live database five days after the indexes landed:

    relname  n_live_tup  n_dead_tup  last_autovacuum
    jobs        350,181      65,084  2026-09-17

Autovacuum had not touched the table for five days, and correctly so: its
trigger is `autovacuum_vacuum_threshold + scale_factor * n_live_tup`, which at
the defaults is `50 + 0.2 * 350,181` = 70,086 dead rows. The table was at
65,084 — 93% of the way there, and still waiting.

That default is the wrong shape for this table. A 20% scale factor means
nothing happens for days and then one pass has to clean 70,000 dead tuples
across the whole heap **and every index**. There are five of those now:
migration 0038 added four, and vacuum must scan all of them on every run. On
the two cores this deploys to, that turns into a periodic stall rather than
background maintenance — which is exactly how it was reported ("postgres is
going crazy with CPU utilisation sometimes").

So: ten times more often, roughly a tenth the work each time. 2% of 350,181 is
about 7,000 dead rows, which vacuums in a fraction of the time and keeps the
visibility map warm enough for index-only scans to stay cheap.

Set per table rather than in `postgresql.conf`, because it is a property of
this table's write pattern — a churny row set behind several indexes — and not
of the server. Nothing else here needs it, and a server-wide change would
alter tables nobody has measured.

Revision ID: 0042
Revises: 0041
"""
from typing import Union

from alembic import op

revision: str = "0042"
down_revision: Union[str, None] = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE jobs SET (
            autovacuum_vacuum_scale_factor = 0.02,
            autovacuum_analyze_scale_factor = 0.02
        )
        """
    )


def downgrade() -> None:
    # RESET, not "set it back to 0.2" — the point is to stop overriding the
    # server default, whatever that happens to be.
    op.execute(
        """
        ALTER TABLE jobs RESET (
            autovacuum_vacuum_scale_factor,
            autovacuum_analyze_scale_factor
        )
        """
    )
