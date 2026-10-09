"""Normalize cleared collection state without discarding any saved work.

The original JSONB mappings encoded Python None as JSON null. SQL IS NOT NULL
then treated completed batches and absent cursors as pending work. Recovery
could repeatedly revisit its oldest cleared batches instead of real payloads.
The application now writes SQL NULL and selects actual nonempty job arrays.
Only JSON null values are changed here; arrays and cursor objects stay intact.
"""
from alembic import op


revision = "0062"
down_revision = "0061"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("UPDATE fetch_board_runs SET payload = NULL WHERE payload = 'null'::jsonb")
    op.execute("UPDATE fetch_board_runs SET cursor = NULL WHERE cursor = 'null'::jsonb")
    op.execute("UPDATE company_boards SET fetch_cursor = NULL WHERE fetch_cursor = 'null'::jsonb")


def downgrade():
    # SQL NULL was valid before this repair too. Turning it into JSON null
    # would restore the bug and cannot distinguish original absent values.
    pass
