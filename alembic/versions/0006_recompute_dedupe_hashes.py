"""Recompute dedupe hashes with normalized company/title/location.

The normalization in app.services.deduplication was strengthened (company
legal suffixes stripped, Sr->senior etc., locations reduced to city/remote),
so every stored hash must be recomputed and pre-existing duplicates merged.
The normalization below is a frozen inline copy — migrations must not import
app code.

Revision ID: 0006
Revises: 0005
"""
import hashlib
import re
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels = None
depends_on = None

_COMPANY_SUFFIXES = frozenset({
    "inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation",
    "co", "company", "gmbh", "bv", "sa", "plc", "pvt", "pte", "holdings",
})
_TITLE_TOKEN_MAP = {"sr": "senior", "jr": "junior", "engr": "engineer", "dev": "developer"}
_TITLE_DROP_TOKENS = frozenset({"remote", "hybrid", "onsite", "urgent", "fulltime"})
_REMOTE_RE = re.compile(r"remote|anywhere|worldwide|work from home|wfh", re.I)


def _tokens(s):
    return re.findall(r"[a-z0-9]+", (s or "").lower())


def _norm_company(company):
    tokens = _tokens(company)
    while len(tokens) > 1 and tokens[-1] in _COMPANY_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def _norm_title(title):
    tokens = [_TITLE_TOKEN_MAP.get(t, t) for t in _tokens(title)]
    return " ".join(t for t in tokens if t not in _TITLE_DROP_TOKENS)


def _norm_location(location):
    text = (location or "").strip()
    if not text:
        return ""
    if _REMOTE_RE.search(text):
        return "remote"
    return " ".join(_tokens(re.split(r"[,;/|]", text)[0]))


def _new_hash(company, title, location):
    payload = f"{_norm_company(company)}|{_norm_title(title)}|{_norm_location(location)}"
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(sa.text(
        "SELECT id, company, title, location FROM jobs ORDER BY fetched_at ASC NULLS LAST, id ASC"
    )).fetchall()
    if not rows:
        return

    groups: dict[str, list] = {}
    for row in rows:
        groups.setdefault(_new_hash(row.company, row.title, row.location), []).append(row.id)

    # Phase 1: park every hash on a unique temp value so phase-2 updates can't
    # transiently collide with not-yet-updated rows.
    conn.execute(sa.text("UPDATE jobs SET dedupe_hash = 'tmp-' || id::text"))

    for new_hash, ids in groups.items():
        keeper = ids[0]
        for dup in ids[1:]:
            # Fold the duplicate's source URLs into the keeper before dropping it.
            conn.execute(sa.text(
                "UPDATE jobs k SET source_urls = ("
                "  SELECT ARRAY(SELECT DISTINCT e FROM unnest(k.source_urls || d.source_urls) AS e)"
                ") FROM jobs d WHERE k.id = :keeper AND d.id = :dup"
            ), {"keeper": keeper, "dup": dup})

            has_application = conn.execute(sa.text(
                "SELECT 1 FROM applications WHERE job_id = :dup LIMIT 1"
            ), {"dup": dup}).first()
            if has_application:
                # Keep rows with user activity; give them a distinct hash.
                conn.execute(sa.text(
                    "UPDATE jobs SET dedupe_hash = :h WHERE id = :dup"
                ), {"h": f"{new_hash[:24]}-{str(dup)[:7]}", "dup": dup})
            else:
                conn.execute(sa.text("DELETE FROM jobs WHERE id = :dup"), {"dup": dup})

        conn.execute(sa.text(
            "UPDATE jobs SET dedupe_hash = :h WHERE id = :keeper"
        ), {"h": new_hash, "keeper": keeper})


def downgrade() -> None:
    # Hash recomputation is not reversible (old hashes are derivable from the
    # same data, but merged/deleted duplicates cannot be restored).
    pass
