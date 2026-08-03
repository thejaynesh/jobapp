"""
A single-holder lock so two fetch cycles can't overlap.

Once a fetch can be triggered by hand, the scheduled cycle and a manual one can
collide — and a cycle takes minutes, so the window is wide. Two at once would
double every outbound request, race on the same inserts, and make the per-source
numbers meaningless.

The lock lives in Redis (already required for Celery) with a TTL, so a worker
killed mid-cycle can't wedge fetching forever.
"""

import logging

from app.config import settings

logger = logging.getLogger(__name__)

LOCK_KEY = "jobapp:fetch:running"
# Comfortably longer than a slow cycle, short enough that a crashed worker
# doesn't block the next scheduled run for long.
DEFAULT_TTL_SECONDS = 3600


def _client():
    import redis
    return redis.Redis.from_url(settings.REDIS_URL, socket_timeout=5)


def acquire(ttl: int = DEFAULT_TTL_SECONDS, token: str = "1") -> bool:
    """
    Claim the lock. False means a fetch is already running.

    If Redis is unreachable we allow the fetch: refusing to work because the
    lock service is down would be worse than the overlap it prevents.
    """
    try:
        return bool(_client().set(LOCK_KEY, token, nx=True, ex=ttl))
    except Exception as exc:
        logger.warning("fetch_lock: cannot reach Redis (%s); proceeding unlocked", exc)
        return True


def release() -> None:
    try:
        _client().delete(LOCK_KEY)
    except Exception as exc:
        logger.warning("fetch_lock: could not release lock: %s", exc)


def state() -> dict:
    """{"running": bool, "seconds_left": int | None} for the UI."""
    try:
        client = _client()
        if not client.exists(LOCK_KEY):
            return {"running": False, "seconds_left": None}
        ttl = client.ttl(LOCK_KEY)
        return {"running": True, "seconds_left": ttl if ttl and ttl > 0 else None}
    except Exception as exc:
        logger.warning("fetch_lock: cannot read lock state: %s", exc)
        return {"running": False, "seconds_left": None, "error": str(exc)}
