"""Record which company board a job was read from: `jobs.board`.

`"greenhouse:stripe"`, set when a board adapter that reads a company's whole
listing in one response inserts the job. It is what lets a posting that has
gone from its board be closed on the next read of that board, for free: the
fetch has just listed everything still open, so a stored posting it did not
list is closed. Until now only the liveness sweep closed anything, one HTTP
request per posting, days behind.

Nullable, no default, and set only on rows the board's own adapter wrote. A
Greenhouse posting first stored from SimplifyJobs keeps that source and its
ID, so comparing it against Greenhouse's IDs would close it wrongly; it is
left to the sweep.

No index. The close reads open jobs for the boards polled in one cycle —
hundreds of boards, one query per 500 — so it is a scan or two per board
cycle, and every index makes every vacuum dearer (0042). Measure before
adding one, as 0043 did.

Adding a nullable column with no default is a catalogue change in Postgres,
not a table rewrite; the deploy stops the workers before migrating anyway.

Revision ID: 0044
Revises: 0043
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0044"
down_revision: Union[str, None] = "0043"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("board", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "board")
