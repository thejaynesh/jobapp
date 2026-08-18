# Backups

A gzipped `pg_dump` every night, written to `/storage/backups` on the VPS and
nowhere else.

## What this protects against, and what it doesn't

It protects against the failures that actually happen to this deployment: a bad
migration, a `DROP TABLE` typed into the wrong shell, a container rebuilt with
the volume detached, a schema change that eats a column. Those are the ones
that have any real probability, and a dump beside the database recovers all of
them in a couple of minutes.

**It does not protect against losing the machine.** If the VPS goes, the
backups go with it. That is a deliberate choice, not an oversight — shipping
copies off-box is a decision about where your data lives, and the code should
not make it quietly. When you want that, the seam is `services.backups.run`:
after `temporary.replace(final)` succeeds you have a verified file and a
filename, which is everything an upload needs.

## How it runs

A beat task, `app.tasks.backup.take_backup`, every `BACKUP_INTERVAL_HOURS`
(default 24). Each run:

1. dumps to a **temporary name** in the backup directory,
2. **verifies** it by decompressing the whole file and finding `pg_dump`'s
   completion marker near the end,
3. renames it into place only then,
4. and **only then** rotates old files.

That order is the whole design. A dump written straight over the target would
mean a failure halfway through destroys last night's copy; rotating before
verifying would mean a retention policy that deletes a good backup to make room
for a broken one.

Settings:

| Setting | Default | |
|---|---|---|
| `BACKUP_ENABLED` | `true` | |
| `BACKUP_DIR` | `/storage/backups` | on the `storage_data` volume |
| `BACKUP_INTERVAL_HOURS` | `24` | |
| `BACKUP_KEEP` | `14` | two weeks — long enough that damage done on a Friday and noticed the Monday after next is still recoverable |

## Checking it

The **System** panel on `/runs` shows the newest backup and its age, and turns
red when it is more than twice the interval old. That staleness check is the
point of the panel: the way a backup job actually fails is not an error, it is
a job that stopped running in March and was noticed in September.

By hand:

```bash
docker compose -f docker-compose.prod.yml exec web python -m app.tasks.backup --status
docker compose -f docker-compose.prod.yml exec web python -m app.tasks.backup --verify
docker compose -f docker-compose.prod.yml exec web python -m app.tasks.backup
```

## Restoring

**This drops and recreates every table.** Stop the workers first, or they will
write into a half-restored schema:

```bash
docker compose -f docker-compose.prod.yml stop worker beat

docker compose -f docker-compose.prod.yml exec -T web \
  sh -c 'gunzip -c /storage/backups/jobapp-YYYYMMDDTHHMMSSZ.sql.gz \
       | psql -v ON_ERROR_STOP=1 "$DATABASE_URL"'

docker compose -f docker-compose.prod.yml start worker beat
```

`--restore` prints this with the newest filename already filled in:

```bash
docker compose -f docker-compose.prod.yml exec web python -m app.tasks.backup --restore
```

`ON_ERROR_STOP=1` is not optional. Without it `psql` exits 0 having skipped
every statement it could not run, so a restore that recreated nothing looks
like it worked — the same failure as an unverified backup, one step later.

Run it from `web` rather than `postgres`: the backup files are on the storage
volume, which only the app containers mount.

## Checking a dump by hand

The completion marker is **not** the last line. `pg_dump` 16.10 and newer wrap
their output in `\restrict` / `\unrestrict` to block psql meta-command
injection (CVE-2025-8714), so a healthy dump ends like this:

```
--
-- PostgreSQL database dump complete
--

\unrestrict ywfxlKs1aa082Kj49Kxyr8e6LeCHJdBJyIOEhG1PhEmHqf32Z2AIjfWOjq1ztD1
```

So `tail -3` shows the `\unrestrict` line and looks alarming. Grep for the
marker instead:

```bash
zcat backup.sql.gz | tail -8                            # eyeball it
zcat backup.sql.gz | grep -c "database dump complete"    # expect 1
```

Or just use the built-in verifier, which does exactly this:

```bash
docker compose -f docker-compose.prod.yml exec web python -m app.tasks.backup --verify
```

One consequence for restores: those are psql meta-commands, so the dump must be
restored with a `psql` at least as new as the `pg_dump` that wrote it. Both come
from `postgresql-client-16` in this image, so that holds here.

## Why `postgresql-client-16` is in the Dockerfile

`pg_dump` refuses to dump a server newer than itself, and Debian bookworm ships
15 against a 16 server. The distro package would give you a backup task that
fails every night with a version-mismatch error — which is worse than no task,
because it looks like one.
