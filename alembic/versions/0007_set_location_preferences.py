"""Set structured location preferences on the existing profile.

The location system moved from free-text target_locations to structured
region preferences; older profiles never got them, which silently limits
searching to the Remote+US fallback. Patch the profile in place so no manual
re-seed is needed: USA, Canada, UK, Europe, Australia, New Zealand + remote.

Revision ID: 0007
Revises: 0006
"""
import json
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels = None
depends_on = None

_PATCH = {
    "location_preferences": {
        "regions": ["usa", "canada", "uk", "europe", "australia", "new_zealand"],
        "remote_ok": True,
        "custom": [],
    },
    "target_locations": [
        "United States", "Canada", "London, United Kingdom", "Europe",
        "Sydney, Australia", "Auckland, New Zealand", "Remote",
    ],
}


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE profiles SET data = data || CAST(:patch AS jsonb) "
            "WHERE id = (SELECT id FROM profiles ORDER BY id LIMIT 1)"
        ),
        {"patch": json.dumps(_PATCH)},
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE profiles SET data = data - 'location_preferences' "
            "WHERE id = (SELECT id FROM profiles ORDER BY id LIMIT 1)"
        )
    )
