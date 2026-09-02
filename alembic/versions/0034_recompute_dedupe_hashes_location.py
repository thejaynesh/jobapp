"""Recompute dedupe hashes after teaching normalize_location about noise.

The location normalizer used to take the first comma-segment verbatim, which
handled "Boston, MA" and "Boston, Massachusetts, United States" and missed
every shape without a comma in the right place:

    "US-MA-Boston"             vs "Boston, MA"
    "United States - New York" vs "New York, NY"
    "Greater Boston Area"      vs "Boston, MA"

Each miss is one posting stored twice, so the stored hashes have to be
recomputed and the duplicates that fall out folded together. Same shape as
0006, which did this when the normalizer was last strengthened, with two
differences.

The first is that `archived_jobs` is recomputed too. It did not exist in 0006.
Leaving it on the old hashes would be worse than doing nothing: `was_archived`
matches on the hash, so every archived posting would stop being recognised,
come back as new on the next fetch, cost a scoring call, reach the same verdict
it reached months ago, and be archived again.

The second is that folding a duplicate into its keeper now carries the
duplicate's data across before dropping it — the description if it is fuller,
and any column the keeper is missing. 0006 kept only the source URLs, which was
right when a merge was defined as "the URL plus maybe the text"; it is not
right now that the whole point of collapsing two rows is that the survivor
knows more than either did.

The normalization below is a frozen inline copy — migrations must not import
app code, or a later edit to the service silently rewrites what this migration
did.

Revision ID: 0034
Revises: 0033
"""
import hashlib
import re
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0034"
down_revision: Union[str, None] = "0033"
branch_labels = None
depends_on = None

_COMPANY_SUFFIXES = frozenset({
    "inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation",
    "co", "company", "gmbh", "bv", "sa", "plc", "pvt", "pte", "holdings",
})
_TITLE_TOKEN_MAP = {"sr": "senior", "jr": "junior", "engr": "engineer", "dev": "developer"}
_TITLE_DROP_TOKENS = frozenset({"remote", "hybrid", "onsite", "urgent", "fulltime"})
_REMOTE_RE = re.compile(r"remote|anywhere|worldwide|work from home|wfh", re.I)
_LOCATION_NOISE = frozenset(
    {"usa", "us", "u", "s", "united", "states", "america"}
    | {"greater", "area", "metro", "metropolitan", "region", "county"}
    | {
        "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id",
        "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms",
        "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok",
        "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv",
        "wi", "wy", "dc",
    }
)


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
    tokens = _tokens(re.split(r"[,;/|]", text)[0])
    kept = [t for t in tokens if t not in _LOCATION_NOISE]
    return " ".join(kept or tokens)


def _new_hash(company, title, location):
    payload = f"{_norm_company(company)}|{_norm_title(title)}|{_norm_location(location)}"
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


# Columns a duplicate may fill in on the row that survives it. Deliberately the
# same set the service merges on a live cross-post, minus the ones it refuses
# to touch — `location` because it is an input to the hash being computed here,
# and `experience_level` because it is defaulted rather than left null.
_FILL_IF_NULL = (
    "apply_url", "posted_at", "employment_type", "salary_currency",
    "required_years", "education_required", "benefits_note", "language",
)


def _fold_urls(conn, table, keeper, dup):
    """
    Union the duplicate's source URLs onto the keeper.

    The one fold the archive needs as much as the job table does: `was_archived`
    matches on a URL first, so an archived posting that lost the address one of
    its sources listed it at comes back as new, buys a scoring call, reaches the
    verdict it already reached, and is archived a second time.
    """
    conn.execute(sa.text(
        f"UPDATE {table} k SET source_urls = ("
        f"  SELECT ARRAY(SELECT DISTINCT e FROM unnest(k.source_urls || d.source_urls) AS e)"
        f") FROM {table} d WHERE k.id = :keeper AND d.id = :dup"
    ), {"keeper": keeper, "dup": dup})


def _fold(conn, keeper, dup):
    """
    Carry the rest of what the duplicate knows onto the keeper.

    The source URLs are already unioned by the caller; this is everything else.
    """
    # Only where the keeper has nothing, and never over a field the user has
    # edited by hand — the same rule every automatic writer follows.
    for column in _FILL_IF_NULL:
        conn.execute(sa.text(
            f"UPDATE jobs k SET {column} = d.{column} FROM jobs d "
            f"WHERE k.id = :keeper AND d.id = :dup "
            f"  AND k.{column} IS NULL AND d.{column} IS NOT NULL "
            f"  AND NOT (:col = ANY(k.manual_fields))"
        ), {"keeper": keeper, "dup": dup, "col": column})

    # Pay moves as a band or not at all: a minimum from one row and a maximum
    # from another is a range nobody ever stated.
    conn.execute(sa.text(
        "UPDATE jobs k SET salary_min = d.salary_min, salary_max = d.salary_max, "
        "                  salary_currency = d.salary_currency "
        "FROM jobs d WHERE k.id = :keeper AND d.id = :dup "
        "  AND k.salary_min IS NULL AND k.salary_max IS NULL "
        "  AND (d.salary_min IS NOT NULL OR d.salary_max IS NOT NULL) "
        "  AND NOT ('salary_min' = ANY(k.manual_fields)) "
        "  AND NOT ('salary_max' = ANY(k.manual_fields))"
    ), {"keeper": keeper, "dup": dup})

    # True can be gained and never lost: the column cannot tell "not remote"
    # from "the source didn't say".
    conn.execute(sa.text(
        "UPDATE jobs k SET is_remote = TRUE FROM jobs d "
        "WHERE k.id = :keeper AND d.id = :dup AND d.is_remote AND NOT k.is_remote"
    ), {"keeper": keeper, "dup": dup})

    # The fuller description wins, which is the same length comparison the
    # service makes, and never over one the user wrote.
    conn.execute(sa.text(
        "UPDATE jobs k SET description = d.description FROM jobs d "
        "WHERE k.id = :keeper AND d.id = :dup AND d.description IS NOT NULL "
        "  AND length(d.description) > coalesce(length(k.description), 0) "
        "  AND NOT ('description' = ANY(k.manual_fields))"
    ), {"keeper": keeper, "dup": dup})


def _regroup(conn, table, fold=None):
    rows = conn.execute(sa.text(
        f"SELECT id, company, title, location FROM {table} "
        f"ORDER BY fetched_at ASC NULLS LAST, id ASC"
    )).fetchall()
    if not rows:
        return

    groups: dict[str, list] = {}
    for row in rows:
        groups.setdefault(_new_hash(row.company, row.title, row.location), []).append(row.id)

    # Park every hash on a unique temp value first, so the updates below cannot
    # transiently collide with rows that have not been updated yet.
    conn.execute(sa.text(f"UPDATE {table} SET dedupe_hash = 'tmp-' || id::text"))

    for new_hash, ids in groups.items():
        keeper = ids[0]
        for dup in ids[1:]:
            _fold_urls(conn, table, keeper, dup)
            if fold is not None:
                fold(conn, keeper, dup)
            has_application = conn.execute(sa.text(
                "SELECT 1 FROM applications WHERE job_id = :dup LIMIT 1"
            ), {"dup": dup}).first() if table == "jobs" else None
            if has_application:
                # A row the user has acted on is never deleted. It keeps a
                # distinct hash so the unique constraint holds.
                conn.execute(sa.text(
                    f"UPDATE {table} SET dedupe_hash = :h WHERE id = :dup"
                ), {"h": f"{new_hash[:24]}-{str(dup)[:7]}", "dup": dup})
            else:
                conn.execute(sa.text(f"DELETE FROM {table} WHERE id = :dup"), {"dup": dup})

        conn.execute(sa.text(
            f"UPDATE {table} SET dedupe_hash = :h WHERE id = :keeper"
        ), {"h": new_hash, "keeper": keeper})


def upgrade() -> None:
    conn = op.get_bind()
    _regroup(conn, "jobs", fold=_fold)
    # No fold for the archive: it stores the verdict, not the posting, and the
    # description is exactly what archiving discarded.
    _regroup(conn, "archived_jobs")


def downgrade() -> None:
    # Not reversible. The old hashes are derivable from the same three columns,
    # but the rows this merged and deleted are not.
    pass
