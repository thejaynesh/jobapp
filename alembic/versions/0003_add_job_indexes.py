"""add indexes on jobs status, source, llm_score, experience_level

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-24 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("ix_jobs_status", "jobs", ["status"])
    op.create_index("ix_jobs_source", "jobs", ["source"])
    op.create_index("ix_jobs_llm_score", "jobs", ["llm_score"])
    op.create_index("ix_jobs_experience_level", "jobs", ["experience_level"])


def downgrade() -> None:
    op.drop_index("ix_jobs_experience_level", table_name="jobs")
    op.drop_index("ix_jobs_llm_score", table_name="jobs")
    op.drop_index("ix_jobs_source", table_name="jobs")
    op.drop_index("ix_jobs_status", table_name="jobs")
