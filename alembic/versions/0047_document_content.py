"""What each generated document says, alongside the file.

A document was a path to a PDF and nothing else. The tailored summary, the
rewritten bullets, the letter and the keyword check lived in memory for the
length of one generation and were gone — so the application page could show a
download link and nothing about what was in it, a result nobody saw sat in the
worker log, and editing one bullet meant regenerating everything.

One nullable JSONB column on `application_documents`, a small table: the
structured content of that version and the checks run on it. Rows written
before this have none and render as they always did.

Revision ID: 0047
Revises: 0046
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0047"
down_revision: Union[str, None] = "0046"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("application_documents",
                  sa.Column("content", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("application_documents", "content")
