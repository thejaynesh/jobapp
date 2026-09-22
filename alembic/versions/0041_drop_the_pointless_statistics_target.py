"""Put `jobs.source_urls` back on the default statistics target.

Migration 0038 ran `ALTER TABLE jobs ALTER COLUMN source_urls SET STATISTICS
1000` to stop the planner guessing at array selectivity. 0038 has been
corrected, but a statistics target is stored on the column, so editing that
file changes nothing for a database that already applied it. This undoes it
where it landed.

The target bought a better row estimate and not a better plan:

    target 1000   ANALYZE 3439 ms   Bitmap Index Scan   est.   1 row
    target  100   ANALYZE  558 ms   Bitmap Index Scan   est. 600 rows

Measured on the same 120,000-row table 0038's numbers came from. Both use the
GIN index, which is the only thing 0038 set out to achieve, and the default is
six times cheaper to collect.

The reason is structural rather than a matter of tuning. A statistics target
governs how many most-common values ANALYZE records, and for an array column
that means most-common *elements*. `source_urls` holds 120,000 elements with
120,000 distinct values — every one unique — so there is no such thing as a
common element here and a larger sample learns nothing from a longer look.
What it does do is read 300 x target rows, which on a real table means the
whole thing, descriptions de-TOASTed. On a 2-core VPS that was minutes of
CPU for no plan change.

The worse estimate is harmless in this codebase: `find_existing_job` asks
`source_urls @> ARRAY[url]` as a single-table `.first()`, with no join for a
600x overestimate to mis-plan.

`-1` means "use the system default", which is what the column had before 0038
and is not the same as pinning it to 100.

Revision ID: 0041
Revises: 0040
"""
from typing import Union

from alembic import op

revision: str = "0041"
down_revision: Union[str, None] = "0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE jobs ALTER COLUMN source_urls SET STATISTICS -1")
    # The 1000-target statistics stay on the column until something replaces
    # them, so this has to re-collect rather than just change the setting.
    # Cheap now, by the whole point of the change.
    op.execute("ANALYZE jobs (source_urls)")


def downgrade() -> None:
    op.execute("ALTER TABLE jobs ALTER COLUMN source_urls SET STATISTICS 1000")
    op.execute("ANALYZE jobs (source_urls)")
