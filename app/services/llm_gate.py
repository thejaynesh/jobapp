"""
One-at-a-time access to a provider that only allows one at a time.

Some endpoints cap concurrent requests rather than throughput — FreeInference
allows exactly one in flight. Nothing about this app is single-threaded: the
worker runs two Celery processes, a matching batch and a document generation
routinely overlap by design, and a fetch cycle expands its search queries with
an LLM call of its own. So the second caller gets a refusal, and a refusal is
indistinguishable from the provider being broken.

A queue rather than a rejection. Waiting costs seconds; falling through to a
paid provider costs money, and dropping the job costs a job. The wait is
bounded, though — past the ceiling the caller is told no and takes the next
provider in its chain, because a worker slot blocked forever behind a wedged
lock is worse than one paid call.

The lock is Redis (already required for Celery) with a TTL, so a process killed
mid-call cannot hold the gate shut. Release is a compare-and-delete: after a TTL
expiry the holder is no longer the owner, and deleting then would open the gate
for someone else's call.
"""

import contextlib
import logging
import time
import uuid

from app.config import settings

logger = logging.getLogger(__name__)

KEY_PREFIX = "jobapp:llm:gate:"

# Longer than the HTTP timeout on a provider call, so the gate is never released
# out from under a request that is still running.
DEFAULT_TTL_SECONDS = 150
# How long a caller waits for its turn. Must exceed one full call or a queue of
# two would time out immediately and every second caller would fall through.
DEFAULT_WAIT_SECONDS = 120
_POLL_INTERVAL = 0.25

# Only our own token may release the lock.
_RELEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


class Busy(Exception):
    """The gate did not open within the wait window."""


def _client():
    import redis

    return redis.Redis.from_url(settings.REDIS_URL, socket_timeout=5)


@contextlib.contextmanager
def hold(name: str, wait: float | None = None, ttl: int | None = None):
    """
    Hold the gate for `name` for the duration of the block.

    Raises Busy if it does not open in time — callers treat that like any other
    provider failure and move down their chain.

    Redis being unreachable fails open. The gate exists to avoid a 429 from a
    provider that already tells us it is busy; refusing to make any LLM call at
    all because the lock service is down trades a recoverable error for a total
    outage.
    """
    key = f"{KEY_PREFIX}{name}"
    token = uuid.uuid4().hex
    ttl = ttl or DEFAULT_TTL_SECONDS
    wait = DEFAULT_WAIT_SECONDS if wait is None else wait

    try:
        client = _client()
    except Exception as exc:
        logger.warning("llm_gate: cannot reach Redis (%s); proceeding ungated", exc)
        yield
        return

    deadline = time.monotonic() + wait
    acquired = False
    while True:
        try:
            acquired = bool(client.set(key, token, nx=True, ex=ttl))
        except Exception as exc:
            logger.warning("llm_gate: Redis failed mid-wait (%s); proceeding ungated", exc)
            yield
            return
        if acquired or time.monotonic() >= deadline:
            break
        time.sleep(_POLL_INTERVAL)

    if not acquired:
        raise Busy(
            f"{name} allows one request at a time and one has been running for "
            f"more than {wait:.0f}s"
        )

    started = time.monotonic()
    try:
        yield
    finally:
        try:
            client.eval(_RELEASE, 1, key, token)
        except Exception as exc:
            # The TTL still clears it; the gate reopens late rather than never.
            logger.warning("llm_gate: could not release %s: %s", name, exc)
        held = time.monotonic() - started
        if held > ttl:
            # The TTL expired while the call was still running, so someone else
            # may have been let in. Worth knowing about: it means TTL is too low
            # for how slow this provider actually is.
            logger.warning(
                "llm_gate: %s held for %.0fs, longer than its %ds lease — "
                "raise the TTL or lower the request timeout", name, held, ttl,
            )


def state(name: str) -> dict:
    """{"busy": bool, "seconds_left": int | None} — for status display."""
    key = f"{KEY_PREFIX}{name}"
    try:
        client = _client()
        if not client.exists(key):
            return {"busy": False, "seconds_left": None}
        ttl = client.ttl(key)
        return {"busy": True, "seconds_left": ttl if ttl and ttl > 0 else None}
    except Exception as exc:
        return {"busy": False, "seconds_left": None, "error": str(exc)}
