"""
What migration 0034 would do to this database, without doing any of it.

0034 recomputes every dedupe hash after the location normalizer was taught to
read "US-MA-Boston" and "Greater Boston Area" as Boston, then folds the
duplicates that fall out of the new grouping. Folding carries the duplicate's
data onto the keeper and then **deletes the duplicate row** — which is correct,
and is also the only irreversible thing in the whole migration, so it is worth
seeing the number before rather than after.

Read-only. Opens one connection, runs three SELECTs, writes nothing.

    docker compose -f docker-compose.prod.yml run --rm web \\
        python scripts/preview_0034.py

The statement count at the end is the useful number for planning. The migration
issues roughly one round trip per group plus eight per folded duplicate, and
`alembic upgrade head` runs inside the web container's startup under a
60-second ceiling — so if that count is large, the migration has to be run by
hand first (see the header of docs/DEPLOYING.md).
"""

import importlib.util
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


def _load_migration():
    """
    Import 0034's own frozen normalizer, not the service's.

    The whole point of the migration carrying its own copy is that a later edit
    to `deduplication.py` cannot silently change what it did. A preview that
    read the service would be previewing a different migration.
    """
    path = (
        pathlib.Path(__file__).resolve().parent.parent
        / "alembic" / "versions" / "0034_recompute_dedupe_hashes_location.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0034", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _engine():
    from sqlalchemy import create_engine

    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL is not set.")
    return create_engine(url)


def _table_exists(conn, table):
    from sqlalchemy import text

    return bool(conn.execute(text("SELECT to_regclass(:t)"), {"t": table}).scalar())


def _preview(conn, mig, table, has_applications):
    from sqlalchemy import text

    rows = conn.execute(text(
        f"SELECT id, company, title, location, dedupe_hash FROM {table} "
        f"ORDER BY fetched_at ASC NULLS LAST, id ASC"
    )).fetchall()

    if not rows:
        print(f"\n{table}: empty.")
        return 0

    groups: dict[str, list] = {}
    for row in rows:
        new_hash = mig._new_hash(row.company, row.title, row.location)
        groups.setdefault(new_hash, []).append(row)

    collapsing = {h: rs for h, rs in groups.items() if len(rs) > 1}
    duplicates = sum(len(rs) - 1 for rs in collapsing.values())

    # A row somebody has acted on is never deleted; it keeps a suffixed hash.
    protected = 0
    if has_applications and duplicates:
        dup_ids = [r.id for rs in collapsing.values() for r in rs[1:]]
        protected = conn.execute(text(
            "SELECT count(DISTINCT job_id) FROM applications WHERE job_id = ANY(:ids)"
        ), {"ids": dup_ids}).scalar() or 0

    unchanged = sum(
        1 for rs in groups.values() if len(rs) == 1 and rs[0].dedupe_hash ==
        mig._new_hash(rs[0].company, rs[0].title, rs[0].location)
    )

    print(f"\n{table}")
    print(f"  rows                        {len(rows):>9,}")
    print(f"  groups after recompute      {len(groups):>9,}")
    print(f"  already on the right hash   {unchanged:>9,}")
    print(f"  groups that collapse        {len(collapsing):>9,}")
    print(f"  duplicates folded in        {duplicates:>9,}")
    print(f"    of those, kept (applied)  {protected:>9,}")
    print(f"    of those, DELETED         {duplicates - protected:>9,}")

    if collapsing:
        print("\n  A sample of what collapses — check these read as the same job:")
        for _, rs in list(collapsing.items())[:12]:
            keeper = rs[0]
            print(f"    {keeper.company} / {keeper.title}")
            for row in rs:
                mark = "keep" if row is keeper else "fold"
                print(f"      [{mark}] {row.location!r}")

    # One UPDATE per group, plus the fold's eight statements per duplicate,
    # plus one park-the-hash UPDATE for the table.
    return len(groups) + duplicates * (8 if has_applications else 1) + 1


def main():
    engine = _engine()
    mig = _load_migration()

    with engine.connect() as conn:
        statements = 0
        statements += _preview(conn, mig, "jobs", has_applications=True)
        if _table_exists(conn, "archived_jobs"):
            statements += _preview(conn, mig, "archived_jobs", has_applications=False)

    print(f"\nRound trips the migration will make: about {statements:,}")
    if statements > 20_000:
        print(
            "\nThat is well past what fits in the 60-second ceiling on the\n"
            "startup migration, so `up -d` alone would fail the upgrade and the\n"
            "app would serve 503. Run `alembic upgrade head` by hand first —\n"
            "see docs/DEPLOYING.md."
        )


if __name__ == "__main__":
    main()
