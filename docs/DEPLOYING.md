# Deploying to the VPS

Push to `main`. The deployment workflow runs the reusable test workflow first,
including the extension's syntax and browser interception checks. A failed test
prevents both image publication and deployment.

GitHub Actions builds the exact tested commit and publishes its image to GHCR.
The VPS pulls the immutable image digest, checks out the matching commit, stops
background workers, runs migrations, and starts the new containers. Docker builds
no longer consume CPU on the VPS. The existing VPS_HOST, VPS_USER and VPS_SSH_KEY
secrets are sufficient; the workflow's short-lived GITHUB_TOKEN authenticates the
registry pull. Registry credentials are held in a temporary directory and removed
when deployment exits.

`scripts/deploy-vps.sh` records two infrastructure values in the VPS `.env`:

- `APP_IMAGE`: the deployed image digest, so later Compose commands use that image.
- `REDIS_DATA_VOLUME`: the exact volume holding the broker's data.

The production Compose file retains a build definition for local development,
but deployment always uses `--no-build`. To build locally, override APP_IMAGE with
an ordinary development tag such as `jobapp-app`; a digest is not a build tag.

## Redis upgrade and persistence

The old deployment used an anonymous Redis data volume. The first new deployment
finds that exact volume and records its name instead of creating an empty broker.
It enables AOF in the running Redis and waits for the initial rewrite to finish
before restarting with AOF enabled. Starting with AOF enabled before that rewrite
could ignore an existing RDB snapshot.

The volume is explicitly referenced as external, so subsequent Compose down/up
cycles reuse it. Do not remove that volume. AOF uses every-second syncing: it
reduces crash-related task loss, but does not promise zero loss after power failure.

For a fresh installation, create the configured external volume before starting:

```bash
docker volume create jobapp_redis_data
docker compose -f docker-compose.prod.yml up -d postgres redis
```

Configure `.env` first, including POSTGRES_PASSWORD and APP_DOMAIN. Existing
installations should let the deployment script detect their volume automatically.

## Readiness and rollback

`/health` remains a liveness/diagnostic endpoint. `/ready` returns success only
when the schema startup check, authentication configuration and database check
succeed. Deployment waits for readiness, reloads Caddy without dropping existing
connections, and verifies that Caddy's container can reach the application.

If migration or readiness fails, the script restores the previous application
image and restarts the application services. This does not downgrade the database;
migrations accompanying a deployment must remain compatible with the previous
image for automatic rollback to be safe. The current delivery/ingestion migrations
only add columns and satisfy that requirement.

```bash
docker compose -f docker-compose.prod.yml exec web alembic current
docker compose -f docker-compose.prod.yml exec web curl -fsS http://localhost:8000/ready
docker compose -f docker-compose.prod.yml logs --tail 50 web worker worker-interactive
```

The image digest and commit are printed in the successful deployment log. If SSH
cannot connect, rerun the failed workflow job after connectivity returns; no new
commit is needed.

## Interrupted work

Celery acknowledges long-running tasks after completion. Its persisted Redis
broker can redeliver interrupted work. Browser results have separate ingestion
state, so a saved result with a processing error retries on the configured schedule
and is retained until processed. Pending counts and recent errors appear on Runs.

Outreach reserves a delivery before contacting SMTP. An ambiguous or interrupted
send is not automatically repeated. Check Sent, then mark it sent or explicitly
confirm a retry from the outreach panel. A still-active delivery cannot be retried.

## Historical migration 0034

A fresh restore from an old backup may need the large deduplication migration.
Before it, take and verify a backup and inspect `scripts/preview_0034.py`. Run the
migration explicitly with workers stopped; the web startup migration has a short
time limit. The deployment script runs migrations separately with a longer limit.
Never delete a running worker's Redis lock to speed deployment: wait for the
worker to finish or for its expiring lock to recover.
