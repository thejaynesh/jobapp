"""Record how each host answers enrichment, not just when it refuses.

`failures_by_host` has always been half the picture, and the wrong half to
decide on. Adzuna produced 86 failures in one pass and is the most productive
source in the table at 51% — it leads the failure column because it leads the
attempt column. Jooble looks the same from that column and is the opposite
case: 2,788 attempts for 13 descriptions.

Nothing on the server enrichment path has ever consulted either. The browser
path honours a paused or challenge-blocked host; `for_server` checks nothing at
all, so a host that has never once produced a description is asked again every
seven days for as long as the backlog exists.

`host_outcomes` is `{host: {"a": attempts, "s": successes}}` — the denominator,
so a rate can be computed rather than a count compared against nothing.

Nullable and unbackfilled. Older runs simply do not contribute to the window,
which is the correct reading: they hold no record of what succeeded, and
inventing one would be a guess about exactly the thing being measured.

Revision ID: 0037
Revises: 0036
"""
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0037"
down_revision: Union[str, None] = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "enrichment_runs",
        sa.Column("host_outcomes", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("enrichment_runs", "host_outcomes")
