"""
A single-holder lock so two fetch cycles can't overlap.

Once a fetch can be triggered by hand, the scheduled cycle and a manual one can
collide — and a cycle takes minutes, so the window is wide. Two at once would
double every outbound request, race on the same inserts, and make the per-source
numbers meaningless.

The lock lives in Redis (already required for Celery) with a TTL, so a worker
killed mid-cycle can't wedge fetching forever.

Model comparisons use the same mechanism under their own key: they don't
conflict with a fetch, so sharing one key would have each block the other.
"""

import logging
import uuid

from app.config import settings

logger = logging.getLogger(__name__)

LOCK_KEY = "jobapp:fetch:running"
COMPARE_LOCK_KEY = "jobapp:compare:running"
# Comfortably longer than a slow cycle, short enough that a crashed worker
# doesn't block the next scheduled run for long.
DEFAULT_TTL_SECONDS = 3600

# Only the holder's own token may release the lock (same script as llm_gate).
# A plain DELETE would let a cycle that outlived its TTL delete the *next*
# cycle's lock, quietly allowing a third to overlap it.
_RELEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""

# The token this process stored for each key it holds. acquire/release pairs
# always run within one worker process, so process-local is the right scope.
_held_tokens: dict[str, str] = {}


def _client():
    import redis
    return redis.Redis.from_url(settings.REDIS_URL, socket_timeout=5)


def acquire(ttl: int = DEFAULT_TTL_SECONDS, token: str | None = None,
            key: str = LOCK_KEY) -> bool:
    """
    Claim the lock. False means a fetch is already running.

    If Redis is unreachable we allow the fetch: refusing to work because the
    lock service is down would be worse than the overlap it prevents.
    """
    token = token or uuid.uuid4().hex
    try:
        acquired = bool(_client().set(key, token, nx=True, ex=ttl))
    except Exception as exc:
        logger.warning("fetch_lock: cannot reach Redis (%s); proceeding unlocked", exc)
        return True
    if acquired:
        _held_tokens[key] = token
    return acquired


def release(key: str = LOCK_KEY) -> None:
    token = _held_tokens.pop(key, None)
    try:
        if token is None:
            # Acquired while Redis was unreachable (or never acquired here):
            # there is no token to compare, and deleting blind could release
            # somebody else's lock — the TTL clears it instead.
            return
        _client().eval(_RELEASE, 1, key, token)
    except Exception as exc:
        logger.warning("fetch_lock: could not release lock: %s", exc)


def state(key: str = LOCK_KEY) -> dict:
    """{"running": bool, "seconds_left": int | None} for the UI."""
    try:
        client = _client()
        if not client.exists(key):
            return {"running": False, "seconds_left": None}
        ttl = client.ttl(key)
        return {"running": True, "seconds_left": ttl if ttl and ttl > 0 else None}
    except Exception as exc:
        logger.warning("fetch_lock: cannot read lock state: %s", exc)
        return {"running": False, "seconds_left": None, "error": str(exc)}
