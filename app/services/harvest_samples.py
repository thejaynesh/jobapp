"""
Keeping a payload we could not read, so that it can be read later.

The harvest reader is shape-based: it walks a response looking for anything
with a title, a company and an identifier. That is why a site nobody wrote a
parser for usually works on the first try — and when it does not, the payload
was thrown away in the same breath. "Forwarding, never finds jobs" was a
verdict with no evidence attached, and the only way to act on it was to open
DevTools yourself and go looking.

This keeps the evidence. Bounded, because the point is diagnosis and not
archival:

* **Truncated.** A payload can be three megabytes; a handful of job objects is
  enough to see how the site names its fields. Trimming is by structure rather
  than by string length, so what is kept is still valid JSON.
* **Capped per host.** A site failing every request would otherwise write a
  sample per response, all of them saying the same thing.
* **Expired.** A sample is worth having until the recipe is written.

And one rule that is about what these actually are: **they are responses to a
logged-in session.** They can carry the user's own name, their account id, the
name of whoever posted a job. That is the user's own data on the user's own
machine, which is why keeping it at all is reasonable — but it is also why it
is trimmed hard, capped, and expired rather than accumulated.
"""

import json
import logging
from datetime import datetime, timedelta, timezone

from app.config import settings

logger = logging.getLogger(__name__)

# How much of one payload to keep. Enough to see the shape and a few real
# values; far short of the whole response.
MAX_SAMPLE_BYTES = 60_000

# How many items to keep from any one array. A search response holds twenty-five
# identical-shaped cards, and three of them teach exactly as much as all of
# them do.
MAX_ARRAY_ITEMS = 4

# How deep to descend. Deeper than the reader's own walk, since the thing being
# diagnosed is often that the interesting object is deeper than expected.
MAX_DEPTH = 14


def _keep() -> int:
    return max(1, int(getattr(settings, "HARVEST_SAMPLES_PER_HOST", 5)))


def _ttl_days() -> int:
    return max(1, int(getattr(settings, "HARVEST_SAMPLE_TTL_DAYS", 30)))


def trim(value, depth: int = 0):
    """
    A structurally smaller copy of `value`, still valid JSON.

    Arrays are cut to the first few items rather than dropped: the shape of a
    list of jobs is the single most useful thing in a payload, and a length is
    not a shape. Strings are truncated because a description can be the whole
    response and teaches nothing about field names.
    """
    if depth > MAX_DEPTH:
        return "…"
    if isinstance(value, dict):
        return {str(k)[:80]: trim(v, depth + 1) for k, v in list(value.items())[:60]}
    if isinstance(value, list):
        return [trim(item, depth + 1) for item in value[:MAX_ARRAY_ITEMS]]
    if isinstance(value, str):
        return value[:600]
    return value


def _fits(payload) -> dict | list:
    """Trim until it is under the byte cap, or give up and keep the shape."""
    trimmed = trim(payload)
    try:
        if len(json.dumps(trimmed)) <= MAX_SAMPLE_BYTES:
            return trimmed
    except (TypeError, ValueError):
        return {"error": "payload was not serialisable"}

    # Still too big: keep only the top level's keys and their types, which is
    # enough to say where to look next.
    if isinstance(trimmed, dict):
        return {
            key: (type(value).__name__ if not isinstance(value, str) else value[:120])
            for key, value in list(trimmed.items())[:60]
        }
    return {"error": "payload too large to sample", "items": len(trimmed or [])}


def record(db, host: str, payload, *, source_url: str = "", found: int = 0,
           note: str = "") -> bool:
    """
    Keep this payload if the host has room. Returns whether one was stored.

    Never raises. This runs inside the harvest, and a sample that could not be
    written must not cost the jobs that were.
    """
    if not host or payload is None:
        return False
    if not bool(getattr(settings, "HARVEST_SAMPLES_ENABLED", True)):
        return False

    try:
        from app.models.harvest_recipe import HarvestSample

        held = (
            db.query(HarvestSample).filter(HarvestSample.host == host).count()
        )
        if held >= _keep():
            return False

        try:
            size = len(json.dumps(payload))
        except (TypeError, ValueError):
            size = 0

        db.add(HarvestSample(
            host=str(host)[:160],
            source_url=(str(source_url)[:1000] or None),
            payload=_fits(payload),
            bytes=size,
            found=int(found or 0),
            note=(str(note)[:200] or None),
        ))
        # Flushed so the count above sees it next time. A failing site sends
        # payload after payload inside one request cycle, and without this the
        # cap never engages — every call counts the rows already committed and
        # none of the ones queued beside it.
        db.flush()
        logger.info(
            "harvest_samples: kept a %d-byte payload from %s that yielded %d job(s)",
            size, host, found,
        )
        return True
    except Exception as exc:
        logger.warning("harvest_samples: could not keep a sample from %s: %s", host, exc)
        return False


def for_host(db, host: str, limit: int = 5) -> list:
    """This host's samples, newest first — what a recipe is proposed from."""
    from app.models.harvest_recipe import HarvestSample

    return (
        db.query(HarvestSample)
        .filter(HarvestSample.host == host)
        .order_by(HarvestSample.created_at.desc())
        .limit(max(1, limit))
        .all()
    )


def hosts(db) -> list[dict]:
    """
    Hosts with samples waiting, and whether anything has been learned yet.

    The worklist: a host here with no active recipe is a site sending payloads
    nobody can read.
    """
    from sqlalchemy import func

    from app.models.harvest_recipe import HarvestRecipe, HarvestSample

    rows = (
        db.query(
            HarvestSample.host,
            func.count(HarvestSample.id),
            func.max(HarvestSample.created_at),
        )
        .group_by(HarvestSample.host)
        .order_by(func.count(HarvestSample.id).desc())
        .all()
    )
    active = {
        row[0] for row in
        db.query(HarvestRecipe.host).filter(HarvestRecipe.status == "active").all()
    }
    return [
        {"host": host, "samples": int(count or 0), "last_seen": last,
         "has_recipe": host in active}
        for host, count, last in rows
    ]


def clear(db, host: str) -> int:
    """Drop a host's samples. Called once a recipe for it is working."""
    from app.models.harvest_recipe import HarvestSample

    removed = (
        db.query(HarvestSample)
        .filter(HarvestSample.host == host)
        .delete(synchronize_session=False)
    )
    db.commit()
    return removed


def prune(db) -> int:
    """Expire samples nobody turned into a recipe. Returns how many went."""
    from app.models.harvest_recipe import HarvestSample

    cutoff = datetime.now(timezone.utc) - timedelta(days=_ttl_days())
    removed = (
        db.query(HarvestSample)
        .filter(HarvestSample.created_at < cutoff)
        .delete(synchronize_session=False)
    )
    db.commit()
    if removed:
        logger.info("harvest_samples: expired %d old sample(s)", removed)
    return removed
