"""Say whether a kept payload was a near miss or a real forward.

Both land in `harvest_samples` and they are not worth the same. A *forward*
named job fields — `"jobTitle"`, `"companyName"` — and the reader still could
not assemble a job out of it, which is exactly what a recipe gets written from.
A *probe* named none of them and is a guess kept in case it matters.

Without the distinction the store could not rank them, so five guesses filled a
host's five slots and the board's own listings were refused for lack of room:
JobRight was represented in the evidence store by five copies of a video SDK's
config, and sixteen attempts to learn a recipe for Handshake all reported
"found no jobs in any sample" — true, and about the samples.

Existing rows default to false, which is the conservative direction. It makes
them look like forwards, so they are treated as the more valuable kind and are
not displaced by a new probe. They will age out on the TTL.

Revision ID: 0036
Revises: 0035
"""
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0036"
down_revision: Union[str, None] = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "harvest_samples",
        sa.Column("probe", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("harvest_samples", "probe")
