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
import time
import uuid

from app.config import settings

logger = logging.getLogger(__name__)

LOCK_KEY = "jobapp:fetch:running"
COMPARE_LOCK_KEY = "jobapp:compare:running"
# Comfortably longer than a slow cycle, short enough that a crashed worker
# doesn't block the next scheduled run for long.
#
# Was an hour, which is far longer than anything it guards — and the TTL is
# not only the crash window. `release` runs a compare-and-delete, and on a
# Redis error it logs and leaves the key to expire; so a blip during release,
# on a cycle that had already finished, blocked the next fetch for up to a
# full hour. Sized to a slow cycle now (the match and enrich locks next door
# use 1,800 for the same reason), and `release` retries before giving up.
DEFAULT_TTL_SECONDS = 1800

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
    if token is None:
        # Acquired while Redis was unreachable (or never acquired here): there
        # is no token to compare, and deleting blind could release somebody
        # else's lock — the TTL clears it instead. A review suggested a plain
        # DELETE here; the comment above `_RELEASE` is why not.
        return
    # Retried, because a single transient error used to cost the next cycle its
    # whole window: the key survives to its TTL, and the next run finds the
    # lock held by a cycle that finished minutes ago.
    for attempt in range(3):
        try:
            _client().eval(_RELEASE, 1, key, token)
            return
        except Exception as exc:
            if attempt == 2:
                logger.warning(
                    "fetch_lock: could not release %s after 3 attempts (%s); "
                    "it expires in at most %ds", key, exc, DEFAULT_TTL_SECONDS,
                )
            else:
                time.sleep(0.5)


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


def any_state(keys) -> dict:
    """
    The same answer as `state`, across several keys: is *anything* running.

    Fetching is no longer one lock. Each group holds its own (see
    `tasks.fetch.GROUP_LOCK_KEYS`) so that the hourly API tier does not queue
    behind a twice-daily browser run — which means "is a fetch running?", the
    question the runs page and the manual trigger both ask, is now a question
    about a set of keys rather than about one.

    `seconds_left` is the longest of the TTLs still standing, because the
    honest answer to "how long until fetching is idle" is the slowest of the
    runs in flight, not the first one to finish.
    """
    running = False
    longest: int | None = None
    try:
        client = _client()
        for key in keys:
            if not client.exists(key):
                continue
            running = True
            ttl = client.ttl(key)
            if ttl and ttl > 0 and (longest is None or ttl > longest):
                longest = ttl
    except Exception as exc:
        logger.warning("fetch_lock: cannot read lock state: %s", exc)
        return {"running": False, "seconds_left": None, "error": str(exc)}
    return {"running": running, "seconds_left": longest}
