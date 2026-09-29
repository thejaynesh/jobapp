"""Which resume went out with an application, and whether a letter did.

Documents are versioned, and the current one is whatever was written last —
so once an application was sent, nothing recorded which version the employer
got, and "do edited resumes get more replies?" or "does a cover letter
help?" could not be asked of the data. `sent_resume_id` is the resume that
was current when the application was marked applied; `sent_cover_letter`
whether a letter was, editable on the application page.

SET NULL: a deleted document version should not take the application's
history with it.

Revision ID: 0051
Revises: 0050
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0051"
down_revision: Union[str, None] = "0050"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("applications", sa.Column(
        "sent_resume_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key("fk_applications_sent_resume_id", "applications",
                          "application_documents", ["sent_resume_id"], ["id"],
                          ondelete="SET NULL")
    op.add_column("applications", sa.Column("sent_cover_letter", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("applications", "sent_cover_letter")
    op.drop_constraint("fk_applications_sent_resume_id", "applications", type_="foreignkey")
    op.drop_column("applications", "sent_resume_id")
