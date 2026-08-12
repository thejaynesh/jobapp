"""The Message-ID a sent message went out with.

Reply detection was unreachable except by clicking a button, so
OUTREACH_AUTO_DRAFT_FOLLOWUPS — on by default — would keep chasing someone who
had already answered. `outreach_sender` was already generating a Message-ID and
putting it on the wire; it just threw it away. Storing it makes a reply a header
match against In-Reply-To/References rather than a heuristic about sender and
subject, which is the difference between deterministic and approximately right.

Revision ID: 0016
Revises: 0015
"""
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "outreach_messages", sa.Column("message_id", sa.String(), nullable=True)
    )
    # The lookup is "which message did this reply quote", one header value at a
    # time, so the index is the whole point of the column.
    op.create_index(
        "ix_outreach_messages_message_id", "outreach_messages", ["message_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_outreach_messages_message_id", table_name="outreach_messages")
    op.drop_column("outreach_messages", "message_id")
