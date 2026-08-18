"""A second opinion on the jobs where the answer is actually in doubt.

One fast model scores everything, and most of its answers are not close calls:
a 20 is a 20 and a 95 is a 95 whoever reads them. The band in the middle is
different — that is where accept and reject actually flip, and where a cheap
model's guess decides whether the user ever sees a job.

So jobs landing in that band get scored again by the strongest configured
provider. Both numbers are kept: the deep one is what the decision uses and
what the UI shows, and the first one stays so the two can be compared — which
is the only way to find out whether the second pass is worth what it costs.

Revision ID: 0025
Revises: 0024
"""
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: Union[str, None] = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("llm_score_deep", sa.Float(), nullable=True))
    op.add_column("jobs", sa.Column("deep_matched_by", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "deep_matched_by")
    op.drop_column("jobs", "llm_score_deep")
