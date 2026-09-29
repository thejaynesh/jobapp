"""How much each scored posting reads like the profile, 0-100.

`services/similarity` computes a TF-IDF cosine between the profile and the
posting before the model is asked to score it. Stored so the matching report
can show, against the user's own decisions, what a pre-screen on it would
have saved in model calls and cost in jobs they applied to — the measurement
that has to come before the pre-screen is switched on.

A nullable column with no default: a catalogue change, not a rewrite of
`jobs`. No index: it is read in aggregate by the report, over the week's
scored jobs, which the `(status, fetched_at)` index already narrows.

Revision ID: 0049
Revises: 0048
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0049"
down_revision: Union[str, None] = "0048"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("similarity", sa.SmallInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "similarity")
