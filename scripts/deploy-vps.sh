#!/usr/bin/env bash
# Run after checking out the tested commit. Never build on the production VPS.
set -Eeuo pipefail

image=${1:?Expected an immutable image reference}
[[ "$image" =~ ^ghcr.io/[a-z0-9_./-]+@sha256:[a-f0-9]{64}$ ]] || { echo "Invalid image digest" >&2; exit 1; }
cd "${DEPLOY_DIR:-/opt/jobapp}"
compose=(docker compose -f docker-compose.prod.yml)
previous_container=$("${compose[@]}" ps -a -q web)
previous_image=""
if [[ -n "$previous_container" ]]; then
  previous_image=$(docker inspect --format '{{.Image}}' "$previous_container")
fi

# Update only deployment-owned infrastructure values; never print .env.
save_env() {
  local key=$1 value=$2 pending
  pending=$(mktemp .env.deploy.XXXXXX)
  awk -v key="$key" -v value="$value" '
    index($0, key "=") == 1 { if (!seen++) print key "=" value; next }
    { print }
    END { if (!seen) print key "=" value }
  ' .env > "$pending"
  chmod --reference=.env "$pending"
  mv -- "$pending" .env
}

credentials=$(mktemp -d)
stopped=false
rollback() {
  result=$?
  trap - EXIT
  rm -rf -- "$credentials"
  if (( result != 0 )) && $stopped && [[ -n "$previous_image" ]]; then
    echo "Deployment failed; restoring previous application image" >&2
    save_env APP_IMAGE "$previous_image"
    "${compose[@]}" up -d --no-build web worker worker-interactive beat || true
  fi
  exit "$result"
}
trap rollback EXIT

printf '%s' "${REGISTRY_TOKEN:?Missing registry token}" |
  docker --config "$credentials" login ghcr.io --username "${REGISTRY_USERNAME:?Missing registry username}" --password-stdin
docker --config "$credentials" pull "$image"

# The old service used an anonymous volume. Preserve that exact volume instead
# of silently replacing its live queues with an empty named volume.
redis_container=$("${compose[@]}" ps -a -q redis)
if [[ -n "$redis_container" ]]; then
  redis_volume=$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}' "$redis_container")
  [[ -n "$redis_volume" ]] || { echo "Redis /data is not a Docker volume; migration needs review" >&2; exit 1; }
  save_env REDIS_DATA_VOLUME "$redis_volume"
  # A stopped old container still owns the queues. Start its original config
  # to load its RDB before enabling AOF; never mistake it for a fresh install.
  if [[ $(docker inspect --format '{{.State.Running}}' "$redis_container") != true ]]; then
    docker start "$redis_container" >/dev/null
  fi
  for ((attempt=0; attempt<30; attempt++)); do
    if docker exec "$redis_container" redis-cli ping | grep -qx PONG; then break; fi
    sleep 2
  done
  # Enable AOF live and wait for its initial rewrite BEFORE restarting with
  # appendonly enabled. Otherwise an existing RDB could be ignored at startup.
  docker exec "$redis_container" redis-cli CONFIG SET appendonly yes | grep -qx OK
  docker exec "$redis_container" redis-cli CONFIG SET appendfsync everysec | grep -qx OK
  aof_ready=false
  for ((attempt=0; attempt<180; attempt++)); do
    persistence=$(docker exec "$redis_container" redis-cli INFO persistence | tr -d '\r')
    if grep -qx 'aof_last_bgrewrite_status:err' <<< "$persistence"; then
      echo "Redis could not create its AOF; leaving running services intact" >&2
      exit 1
    fi
    if grep -qx 'aof_enabled:1' <<< "$persistence" &&
       grep -qx 'aof_rewrite_in_progress:0' <<< "$persistence" &&
       grep -qx 'aof_rewrite_scheduled:0' <<< "$persistence" &&
       grep -qx 'aof_last_bgrewrite_status:ok' <<< "$persistence"; then
      aof_ready=true
      break
    fi
    sleep 2
  done
  $aof_ready || { echo "Redis AOF is not ready; leaving running services intact" >&2; exit 1; }
else
  # Preserve an explicitly configured volume on a fresh host/stack recreation.
  redis_volume=$(sed -n 's/^REDIS_DATA_VOLUME=//p' .env | tail -n 1)
  redis_volume=${redis_volume:-jobapp_redis_data}
  docker volume create "$redis_volume" >/dev/null
  save_env REDIS_DATA_VOLUME "$redis_volume"
fi

"${compose[@]}" stop worker worker-interactive beat
stopped=true
"${compose[@]}" up -d postgres redis
# --no-deps avoids replacing Redis before its data has been preserved.
APP_IMAGE="$image" "${compose[@]}" run --rm --no-deps web alembic upgrade head
save_env APP_IMAGE "$image"
# Let the web processes finish schema checks and startup before the workers
# begin importing task modules and draining the queues on the same small VPS.
# --no-deps keeps this step from starting another application service early.
"${compose[@]}" up -d --no-build --no-deps web

ready=false
for ((attempt=0; attempt<60; attempt++)); do
  if "${compose[@]}" exec -T web curl --fail --silent --max-time 5 http://localhost:8000/ready >/dev/null; then
    ready=true
    break
  fi
  sleep 5
done
$ready || { echo "Application did not become ready" >&2; exit 1; }
"${compose[@]}" up -d --no-build --no-deps caddy
# A new proxy may still be starting its admin listener. Reload preserves
# established connections; retry brief startup races before rolling back.
proxy_ready=false
for ((attempt=0; attempt<30; attempt++)); do
  if "${compose[@]}" exec -T caddy caddy reload --config /etc/caddy/Caddyfile &&
     "${compose[@]}" exec -T caddy wget -q -T 10 -O /dev/null http://web:8000/ready; then
    proxy_ready=true
    break
  fi
  sleep 2
done
$proxy_ready || { echo "Proxy did not become ready" >&2; exit 1; }

# Restore work people are waiting on before batch processing; start the
# scheduler last so it cannot add more work while the application warms up.
# Keep rollback armed until every service has started successfully.
"${compose[@]}" up -d --no-build --no-deps worker-interactive
"${compose[@]}" up -d --no-build --no-deps worker
"${compose[@]}" up -d --no-build --no-deps beat
echo "Deployed ${DEPLOY_SHA:-unknown} as $image; readiness passed"
stopped=false
