"""Bounded, fail-open admission control. Never changes worker resource limits."""
import json
import math
import time
from contextvars import ContextVar
from pathlib import Path

from app.services.tunables import value

PREFIX = "jobapp:capacity:"
DATABASE_TIME = ContextVar("request_database_time", default=None)


def install_database_metrics(engine):
    from sqlalchemy import event
    def before(conn, cursor, statement, parameters, context, executemany):
        context._capacity_started = time.monotonic()
    def after(conn, cursor, statement, parameters, context, executemany):
        observation = DATABASE_TIME.get()
        if observation is not None:
            observation["ms"] += (time.monotonic() - context._capacity_started) * 1000
    if not getattr(engine, "_capacity_metrics_installed", False):
        event.listen(engine, "before_cursor_execute", before)
        event.listen(engine, "after_cursor_execute", after)
        engine._capacity_metrics_installed = True


def _client():
    import redis
    from app.config import settings
    return redis.Redis.from_url(settings.REDIS_URL, socket_connect_timeout=0.15,
                                socket_timeout=0.15, decode_responses=True)


def observe_latency(milliseconds, status_code=200, database_ms=None):
    try:
        redis = _client()
        with redis.pipeline() as pipe:
            pipe.lpush(PREFIX + "pages", json.dumps([time.time(), round(milliseconds, 1), status_code, database_ms]))
            pipe.ltrim(PREFIX + "pages", 0, 99)
            pipe.expire(PREFIX + "pages", 600)
            pipe.execute()
    except Exception:
        pass


def pressure():
    """Linux PSI describes time stalled, not merely a busy CPU. Missing is unknown."""
    result = {"cpu_pressure": None, "memory_pressure": None, "cpu_steal": None}
    for resource in ("cpu", "memory"):
        try:
            line = Path(f"/proc/pressure/{resource}").read_text().splitlines()[0]
            values = dict(pair.split("=") for pair in line.split()[1:])
            result[resource + "_pressure"] = float(values["avg60"])
        except (OSError, ValueError, IndexError, KeyError):
            pass
    return result


def metrics(redis, now):
    result = pressure()
    pages = []
    database, errors = [], 0
    for raw in redis.lrange(PREFIX + "pages", 0, 99):
        try:
            row = json.loads(raw)
            at, duration = row[:2]
            if now - 300 <= at <= now and math.isfinite(duration):
                pages.append(duration)
                errors += int(len(row) > 2 and row[2] >= 500)
                if len(row) > 3 and isinstance(row[3], (int, float)):
                    database.append(row[3])
        except (ValueError, TypeError):
            continue
    result["page_samples"] = len(pages)
    result["latency_p95_ms"] = sorted(pages)[math.ceil(len(pages) * .95) - 1] if pages else None
    result["database_p95_ms"] = sorted(database)[math.ceil(len(database) * .95) - 1] if database else None
    result["response_5xx_count"] = errors
    result["interactive_queue_seconds"] = None
    # Celery's Redis FIFO is LPUSH / BRPOP. Inspect at most four priority lists,
    # without pulling or acknowledging a message.
    for suffix in ("", "\x06\x163", "\x06\x166", "\x06\x169"):
        raw = redis.lindex("interactive" + suffix, -1)
        if raw:
            try:
                at = json.loads(raw).get("headers", {}).get("jobapp_queued_at")
                if isinstance(at, (int, float)) and at <= now:
                    result["interactive_queue_seconds"] = max(result["interactive_queue_seconds"] or 0, now - at)
            except (ValueError, TypeError):
                pass
    try:
        cpu = [int(n) for n in Path("/proc/stat").read_text().splitlines()[0].split()[1:9]]
        previous = redis.get(PREFIX + "cpu")
        redis.set(PREFIX + "cpu", json.dumps(cpu), ex=600)
        if previous:
            previous = json.loads(previous)
            elapsed = sum(cpu) - sum(previous)
            if elapsed > 0:
                result["cpu_steal"] = max(0, 100 * (cpu[7] - previous[7]) / elapsed)
    except (OSError, ValueError, IndexError, TypeError):
        pass
    return result


def reasons(sample, profile):
    found = []
    if sample.get("page_samples", 0) >= 5 and (sample.get("latency_p95_ms") or 0) > value(profile, "adaptive_latency_ms"):
        found.append("recent page latency")
    if sample.get("page_samples", 0) >= 5 and (sample.get("database_p95_ms") or 0) > value(profile, "adaptive_latency_ms"):
        found.append("database latency")
    if (sample.get("interactive_queue_seconds") or 0) > value(profile, "adaptive_queue_seconds"):
        found.append("waiting interactive work")
    for key in ("cpu_pressure", "memory_pressure", "cpu_steal"):
        if (sample.get(key) or 0) >= value(profile, "adaptive_pressure_percent"):
            found.append(key.replace("_", " "))
    return found


def status(profile=None):
    if profile is None:
        from app.services.tunables import _load_profile_data
        profile = _load_profile_data()
    if not value(profile, "adaptive_work_enabled"):
        return {"paused": False, "reason": "Capacity protection disabled", "metrics": {}}
    try:
        redis = _client()
        cached = redis.get(PREFIX + "status")
        if cached:
            return json.loads(cached)
        now = time.time()
        sample = metrics(redis, now)
        why = reasons(sample, profile)
        if why:
            redis.set(PREFIX + "pause", ", ".join(why), ex=int(value(profile, "adaptive_cooldown_seconds")))
        pause = redis.get(PREFIX + "pause")
        result = {"paused": bool(pause), "reason": pause or "Background admission available",
                  "metrics": sample, "observed_at": now, "cooldown_seconds": max(0, redis.ttl(PREFIX + "pause"))}
        redis.set(PREFIX + "status", json.dumps(result), ex=15)
        return result
    except Exception:
        return {"paused": False, "reason": "Capacity telemetry unavailable", "metrics": {}}


def allow_background(profile=None):
    return not status(profile)["paused"]


def source_waits(db, cfg, now):
    """Back off only after three completed, empty-yield probes; always probe again."""
    from datetime import timedelta
    from app.models.fetch_run import FetchRun, FetchSourceRun
    if not cfg.ADAPTIVE_SOURCE_ENABLED:
        return {}
    rows = (db.query(FetchSourceRun, FetchRun.finished_at).join(FetchRun)
            .filter(FetchRun.finished_at >= now - timedelta(days=7), FetchSourceRun.enabled.is_(True),
                    FetchSourceRun.status.in_(["ok", "empty"]))
            .order_by(FetchRun.finished_at.desc()).limit(500).all())
    by_source = {}
    for row, at in rows:
        by_source.setdefault(row.source, []).append((row, at))
    waits = {}
    for source, samples in by_source.items():
        streak = 0
        for row, _ in samples:
            if row.inserted or row.merged:
                break
            streak += 1
        if streak < 3:
            continue
        hours = min(float(cfg.ADAPTIVE_SOURCE_MAX_HOURS), 2 ** min(streak - 2, 8))
        due = samples[0][1] + timedelta(hours=hours)
        if due > now:
            waits[source] = f"Adaptive polling: {streak} probes with no new or updated postings; next probe by {due.isoformat()} (manual runs bypass)."
    return waits
