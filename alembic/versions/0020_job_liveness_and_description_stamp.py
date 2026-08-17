"""Whether a posting is still up, and when its description got fuller.

Two additions with the same motive — the pipeline never looked at a stored
job again:

* Liveness. 3,000+ applications sit ready while their postings quietly close
  on the employer's side. `closed_at`/`closed_note` record that a check found
  a posting gone, and `liveness_checked_at` paces the checks so the same job
  is not fetched every cycle.

* `description_updated_at`. When a merge, harvest, or enrichment replaces a
  description with a meaningfully fuller one, documents generated before that
  moment were grounded in the thinner text. The stamp is what lets the UI say
  "your resume predates the real job description" instead of nobody knowing.

Revision ID: 0020
Revises: 0019
"""
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "jobs", sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("jobs", sa.Column("closed_note", sa.String(), nullable=True))
    op.add_column(
        "jobs",
        sa.Column("liveness_checked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "jobs",
        sa.Column("description_updated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("jobs", "description_updated_at")
    op.drop_column("jobs", "liveness_checked_at")
    op.drop_column("jobs", "closed_note")
    op.drop_column("jobs", "closed_at")
