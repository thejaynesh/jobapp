"""
The matching cycle's LLM call budget, shared across the batches it is cut into.

`MAX_PAID_MATCH_CALLS_PER_CYCLE` (150) and `DEEP_MATCH_MAX_PER_CYCLE` (100) are
the two ceilings that stop a NIM outage from turning into a surprise bill and a
band of borderline jobs from doubling every score. They were counted in a plain
dict created inside `match_all_new_jobs` — which was right when a cycle was one
call, and stopped being right when matching was cut into chained batches of
`MATCH_MAX_JOBS_PER_TASK` (25).

A batch of 25 cannot spend 150 paid calls, so the dict was reset to zero 25 jobs
before either cap could ever be reached, and both were unreachable by
arithmetic. A ten-thousand-job backlog with NIM down would have made four
hundred batches of paid calls, each one convinced it was the first.

So the counters live where the batches can all see them. Redis, because it is
already required for Celery and already holds the lock that guarantees only one
pass runs at a time — which is what makes read-at-start, write-at-end exact
rather than racy.

Two deliberate choices about failure. If Redis is unreachable the budget reads
as empty: matching that refuses to work because the accounting service is down
is worse than the overspend it was guarding against, and the same reasoning
`fetch_lock.acquire` uses. And the counters carry a TTL as well as an explicit
clear, so a chain killed halfway through does not leave the next cycle starting
against a spent budget forever.
"""

import logging

from app.config import settings

logger = logging.getLogger(__name__)

KEY = "jobapp:match:budget"

# Long enough to cover a chain grinding through a large backlog, short enough
# that a chain killed mid-flight does not hold the next one down for a shift.
TTL_SECONDS = 3600

FIELDS = ("paid_calls", "deep_calls")


def _client():
    import redis
    return redis.Redis.from_url(settings.REDIS_URL, socket_timeout=5)


def empty() -> dict[str, int]:
    return {field: 0 for field in FIELDS}


def load() -> dict[str, int]:
    """What this cycle has spent so far. Zeros when Redis cannot be reached."""
    budget = empty()
    try:
        stored = _client().hgetall(KEY) or {}
    except Exception as exc:
        logger.warning("match_budget: cannot reach Redis (%s); counting from zero", exc)
        return budget
    for field in FIELDS:
        raw = stored.get(field.encode()) or stored.get(field)
        try:
            budget[field] = int(raw)
        except (TypeError, ValueError):
            continue
    return budget


def save(budget: dict) -> None:
    """Store what the batch just spent, and push the expiry out."""
    try:
        client = _client()
        client.hset(KEY, mapping={f: int(budget.get(f, 0) or 0) for f in FIELDS})
        client.expire(KEY, TTL_SECONDS)
    except Exception as exc:
        logger.warning("match_budget: could not record the cycle's spend: %s", exc)


def clear() -> None:
    """The chain has stopped: the next cycle starts fresh."""
    try:
        _client().delete(KEY)
    except Exception as exc:
        logger.warning("match_budget: could not reset the cycle budget: %s", exc)
