# Deploying while the pipeline is running

Short version: it is safe, and the system is arranged for it. You do not need
to stop anything.

```bash
cd /opt/jobapp
git pull
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml up -d
docker compose -f docker-compose.prod.yml exec web alembic current
```

`build` does not touch running containers. `up -d` recreates only the services
whose image changed — Postgres and Redis are left alone, so the database never
goes down.

## Why an interrupted task is not lost

`celery_app.py` sets `task_acks_late=True` and `task_reject_on_worker_lost=True`
for exactly this case: a task is acknowledged when it finishes, not when it is
received, so a worker stopped mid-task puts the work back on the queue instead
of taking it away. The trade is at-least-once delivery, which every task here
is safe under — matching re-scores a job, enrichment re-fetches a description,
and nothing sends mail unattended.

Enrichment and matching each commit once at the end of a pass, so an
interrupted pass rolls back rather than leaving half-written state.

## Why the workers get 120 seconds to stop

Celery reads SIGTERM as a warm shutdown: stop accepting work, finish what is in
hand, exit. Docker's default is to wait ten seconds and then SIGKILL — shorter
than an enrichment pass or a document generation, so every deploy used to kill
one mid-flight.

A hard kill loses no work, but it skips the `finally` block that releases the
Redis lock, and those locks have a **thirty-minute TTL**. The visible result was
half an hour of enrichment logging `already running` and doing nothing, which
reads exactly like a broken deploy. `stop_grace_period: 120s` on the worker
lets the task finish instead.

If you ever do end up with a stuck lock — a hard `kill -9`, an OOM, a host
reboot — clear it rather than waiting:

```bash
docker compose -f docker-compose.prod.yml exec redis \
  redis-cli DEL jobapp:enrich:running jobapp:match:running
```

## Migration 0034 needs running by hand

`alembic upgrade head` runs inside the web container's startup with a
**60-second ceiling** (`app/main.py`), and 0034 is the first migration in this
project that cannot finish inside it. It recomputes every hash in `jobs` and
`archived_jobs`, then folds the duplicates the new grouping produces — one
round trip per group plus eight per folded duplicate, which on a six-figure
table is tens of thousands of statements.

Nothing is damaged if you let it fail: the subprocess is killed, Postgres rolls
the whole migration back, and the database is exactly as it was. But the app
serves 503 until the upgrade completes, so run it outside that window instead:

```bash
cd /opt/jobapp
git pull

# 1. See what it will collapse. Read-only, writes nothing.
docker compose -f docker-compose.prod.yml run --rm web python scripts/preview_0034.py

# 2. Take a backup. 0034 DELETES the duplicate rows it folds, and that is the
#    only irreversible thing in it.
docker compose -f docker-compose.prod.yml exec web python -m app.tasks.backup
docker compose -f docker-compose.prod.yml exec web python -m app.tasks.backup --verify

# 3. Quiet the writers, build, migrate with no clock on it, then start.
docker compose -f docker-compose.prod.yml stop worker beat
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml run --rm web alembic upgrade head
docker compose -f docker-compose.prod.yml up -d
```

Step 3's `run --rm web` starts a throwaway container with no timeout around it,
so the migration takes as long as it takes. By the time `up -d` starts the real
web container the schema is already at head and its startup upgrade returns
immediately.

## The one case worth stopping for

A migration that builds an index on a large table takes a lock on it while it
runs, and `alembic upgrade head` runs inside the web container's startup with a
60-second ceiling. On `jobs` — a six-figure table that enrichment and matching
are both writing to — a busy moment can make that slower than it needs to be.

If a release notes a migration touching `jobs`, quiet the writers first:

```bash
docker compose -f docker-compose.prod.yml stop worker beat
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml up -d
```

`up -d` starts them again. This also removes the brief window where a new
worker is running against a schema the web container has not migrated yet —
those tasks error and retry, which is harmless but noisy in the logs.

## Checking a deploy landed

```bash
docker compose -f docker-compose.prod.yml exec web alembic current
docker compose -f docker-compose.prod.yml logs web | grep -i "alembic\|MIGRATION"
docker compose -f docker-compose.prod.yml logs --tail 50 worker
```

The app refuses to serve (503) rather than half-working if migrations fail, so
a green `/health` and the expected revision are the whole check.
