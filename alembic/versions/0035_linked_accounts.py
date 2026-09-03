"""A board's credential, so the server can call its API without a browser.

Tsenta's API takes a Firebase ID token that lasts an hour. The extension reads
one out of the page and sweeps from inside the tab, which works and which
requires a laptop to be open — so the board is only ever as current as the last
time somebody happened to be browsing.

The durable half of that credential is the refresh token, and Google's
`securetoken` endpoint mints fresh ID tokens from it for as long as it lives.
Storing it here is what turns a browser-bound sweep into a scheduled one.

One row per site, and a table rather than a key on the profile blob because the
refresh token rotates: this is written by a Celery task while the web process
writes the same blob for settings and caches, which is the lost-update bug this
project has already fixed once.

Revision ID: 0035
Revises: 0034
"""
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0035"
down_revision: Union[str, None] = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "linked_accounts",
        sa.Column("site", sa.String(), primary_key=True),
        sa.Column("api_key", sa.String(), nullable=False),
        sa.Column("refresh_token", sa.Text(), nullable=False),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_minted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("linked_accounts")
