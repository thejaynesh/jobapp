"""A public profile URL on contacts, for sources that aren't LinkedIn.

GitHub org members and team-page finds come with a profile worth keeping —
`linkedin_url` is the wrong home for a github.com/janedoe page.

Revision ID: 0013
Revises: 0012
"""
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("contacts", sa.Column("profile_url", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("contacts", "profile_url")
