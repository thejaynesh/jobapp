"""Remember which fields the user set by hand.

Everything in `jobs` is written by something automatic — a source adapter, a
cross-post merge, the harvest, an enrichment pass — and all of them decide what
to keep by comparing lengths or checking for null. That is the right rule
between two machines. It is the wrong rule against a person: a description
pasted from the posting the user was actually reading loses to any longer blob
of boilerplate the next enrichment pass scrapes off a listing page.

So a hand-edited field is named here, and every writer checks the list before
overwriting. A list rather than one flag per column because the set of editable
fields will grow, and a `description_is_manual` boolean would have to be joined
by a `location_is_manual` and a `salary_is_manual` behind it.

`edited_at` is separate from `description_updated_at`: that column means "the
text got fuller and documents written against the old one are stale", which is
just as true of an edit as of an enrichment, so an edit sets both. This one
answers "did a person touch this row", which nothing else records.

Revision ID: 0030
Revises: 0029
"""
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0030"
down_revision: Union[str, None] = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column(
            "manual_fields",
            postgresql.ARRAY(sa.String()),
            nullable=False,
            server_default="{}",
        ),
    )
    op.add_column(
        "jobs",
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("jobs", "edited_at")
    op.drop_column("jobs", "manual_fields")
