"""
A nightly copy of the database, on this machine and nowhere else.

Everything else in this repo assumes the data survives. Six months of scored
jobs, the applications, the score history that makes enrichment measurable —
none of it exists anywhere but one Postgres volume on one VPS, and the failure
that ends this project is not a bad prompt, it is a disk.

This is deliberately the modest version. A dump on the same host protects
against the failures that actually happen to a single-user deployment: a bad
migration, a `DROP TABLE` in the wrong shell, a container rebuilt with the
volume detached, a schema change that eats a column. It does **not** protect
against losing the machine. That is a real gap and it is left open on purpose
rather than by oversight — shipping copies off-box is a decision about where
your data lives, and it is not one this code should make quietly.

Three rules, all learned from backups that turned out not to be backups:

* **A dump is not a backup until it has been read back.** Every file is
  verified after writing — decompressed, and checked for the marker `pg_dump`
  writes on its last line. A truncated dump is worse than no dump, because it
  looks like one.
* **Never overwrite the previous one.** The dump is written to a temporary name
  and renamed into place only once it has passed. A backup job that fails
  halfway through must leave last night's file exactly where it was.
* **Silence is the enemy.** The most common way this fails is not an error, it
  is a job that stopped running in March and was noticed in September. The last
  successful run and its age are on the Runs page, and the age is what the page
  actually judges.
"""

import gzip
import logging
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

from app.config import settings

logger = logging.getLogger(__name__)

STATE_KEY = "backup"
FILE_PREFIX = "jobapp-"
FILE_SUFFIX = ".sql.gz"
# What `pg_dump` writes as its last line. Its presence is the difference
# between a complete dump and a file that stopped when the disk filled.
COMPLETION_MARKER = "PostgreSQL database dump complete"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def directory() -> Path:
    return Path(getattr(settings, "BACKUP_DIR", "/storage/backups"))


def _keep() -> int:
    try:
        return max(1, int(getattr(settings, "BACKUP_KEEP", 14)))
    except (TypeError, ValueError):
        return 14


def enabled() -> bool:
    return bool(getattr(settings, "BACKUP_ENABLED", True))


def _dsn_environment(url: str) -> tuple[list[str], dict]:
    """
    `pg_dump` arguments and environment for a SQLAlchemy database URL.

    The password goes in the environment rather than the argument list, because
    an argument is visible in `ps` to anything else on the box — and unlike most
    of this repo's security posture, that one costs nothing to get right.
    """
    parsed = urlparse(url)
    args = ["--dbname", (parsed.path or "/").lstrip("/") or "postgres"]
    if parsed.hostname:
        args += ["--host", parsed.hostname]
    if parsed.port:
        args += ["--port", str(parsed.port)]
    if parsed.username:
        args += ["--username", unquote(parsed.username)]
    env = dict(os.environ)
    if parsed.password:
        env["PGPASSWORD"] = unquote(parsed.password)
    return args, env


def _dump(target: Path) -> None:
    """Write a gzipped logical dump to `target`. Raises on any failure."""
    args, env = _dsn_environment(settings.DATABASE_URL)
    command = [
        "pg_dump",
        # Plain SQL rather than the custom format: it can be read, grepped and
        # partially recovered with nothing but zcat, which matters most in
        # exactly the situation where you are restoring from it.
        "--format=plain",
        # So a restore into a fresh database does not fail on the first table
        # that already exists.
        "--clean",
        "--if-exists",
        "--no-owner",
        "--no-privileges",
        *args,
    ]
    # Streamed rather than captured: a dump of this database is hundreds of
    # megabytes of SQL, and `subprocess.run(capture_output=True)` would hold
    # every byte of it in the worker's memory on the way past.
    with gzip.open(target, "wb") as handle:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env
        )
        if process.stdout is not None:
            shutil.copyfileobj(process.stdout, handle)
            process.stdout.close()
        stderr = (process.stderr.read() if process.stderr else b"").decode(
            "utf-8", "replace"
        )
        if process.stderr:
            process.stderr.close()
        code = process.wait()
    if code != 0:
        raise RuntimeError(
            f"pg_dump exited {code}: {stderr.strip()[:400] or 'no message'}"
        )


def verify(path: Path) -> int:
    """
    Read the file back. Returns the uncompressed size; raises if it is not a
    complete dump.

    Both halves matter. Decompressing catches a file truncated mid-write, and
    the completion marker catches a `pg_dump` that exited cleanly having
    written only part of the schema — which is what a permissions problem on
    one table looks like.
    """
    size = 0
    # Only the end matters, and holding the whole dump in memory to find it
    # would defeat streaming it out in the first place.
    tail = b""
    with gzip.open(path, "rb") as handle:
        while chunk := handle.read(1 << 20):
            size += len(chunk)
            tail = (tail + chunk)[-2048:]
    text = tail.decode("utf-8", "replace")
    if COMPLETION_MARKER not in text:
        last = next(
            (line for line in reversed(text.splitlines()) if line.strip()), ""
        )
        raise RuntimeError(
            "the dump does not end with pg_dump's completion marker, so it is "
            "incomplete — the last line was: " + (last.strip()[:200] or "(empty)")
        )
    return size


def rotate(keep: int | None = None) -> list[str]:
    """Delete all but the newest `keep` backups. Returns what went."""
    keep = _keep() if keep is None else max(1, keep)
    files = existing()
    removed = []
    for path in files[keep:]:
        try:
            path.unlink()
            removed.append(path.name)
        except OSError as exc:
            logger.warning("backups: could not remove %s: %s", path.name, exc)
    return removed


def existing() -> list[Path]:
    """Every backup on disk, newest first."""
    folder = directory()
    if not folder.is_dir():
        return []
    files = [
        path for path in folder.iterdir()
        if path.is_file() and path.name.startswith(FILE_PREFIX)
        and path.name.endswith(FILE_SUFFIX)
    ]
    return sorted(files, key=lambda path: path.name, reverse=True)


def run(db=None) -> dict:
    """
    Take one backup. Returns what happened; never raises.

    Rotation runs only after a verified success. Deleting an old backup to make
    room for one that then fails is how a retention policy turns into data
    loss.
    """
    if not enabled():
        return {"ok": False, "skipped": True, "detail": "Backups are switched off."}

    folder = directory()
    started = _now()
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return _finish(db, {"ok": False, "started_at": started,
                            "error": f"cannot write to {folder}: {exc}"})

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    final = folder / f"{FILE_PREFIX}{stamp}{FILE_SUFFIX}"

    # Written under a temporary name in the same directory, so a failure
    # anywhere below leaves last night's backup untouched and no half-file
    # looking like a real one. Same directory so the rename is atomic.
    handle, temporary_name = tempfile.mkstemp(
        dir=str(folder), prefix=f"{FILE_PREFIX}partial-", suffix=FILE_SUFFIX
    )
    os.close(handle)
    temporary = Path(temporary_name)

    try:
        _dump(temporary)
        uncompressed = verify(temporary)
        temporary.replace(final)
    except FileNotFoundError:
        temporary.unlink(missing_ok=True)
        return _finish(db, {
            "ok": False, "started_at": started,
            "error": "pg_dump is not installed in this image — the backup task "
                     "cannot run without the postgresql-client package.",
        })
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        logger.error("backups: %s", exc)
        return _finish(db, {"ok": False, "started_at": started, "error": str(exc)[:500]})

    removed = rotate()
    result = {
        "ok": True,
        "started_at": started,
        "finished_at": _now(),
        "file": final.name,
        "bytes": final.stat().st_size,
        "uncompressed_bytes": uncompressed,
        "removed": removed,
        "kept": len(existing()),
    }
    logger.info(
        "backups: wrote %s (%.1f MB compressed, %.1f MB of SQL), keeping %d, "
        "removed %d", final.name, result["bytes"] / 1e6, uncompressed / 1e6,
        result["kept"], len(removed),
    )
    return _finish(db, result)


def _finish(db, result: dict) -> dict:
    """Record the outcome where the Runs page can read it."""
    if db is None:
        return result
    try:
        from app.models.profile import Profile

        profile = db.query(Profile).first()
        if profile is None:
            return result
        state = dict(profile.data or {}).get(STATE_KEY) or {}
        # A failure must not erase the memory of the last success — "it worked
        # in March and has failed every night since" is the whole story, and
        # either half alone tells a misleading version of it.
        merged = {
            "last_attempt": result.get("finished_at") or result.get("started_at"),
            "last_error": None if result.get("ok") else result.get("error"),
            "last_success": (
                result.get("finished_at") if result.get("ok")
                else state.get("last_success")
            ),
            "last_file": result.get("file") if result.get("ok") else state.get("last_file"),
            "last_bytes": result.get("bytes") if result.get("ok") else state.get("last_bytes"),
        }
        data = dict(profile.data or {})
        data[STATE_KEY] = merged
        profile.data = data
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.warning("backups: could not record the outcome: %s", exc)
    return result


def status(db=None) -> dict:
    """
    What the Runs page shows.

    The files on disk are the source of truth for what exists, because a table
    can disagree with the disk and the disk cannot disagree with itself. The
    stored state adds the one thing a directory listing cannot say: that last
    night's attempt failed.
    """
    files = existing()
    newest = files[0] if files else None
    age_hours = None
    if newest is not None:
        modified = datetime.fromtimestamp(newest.stat().st_mtime, tz=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - modified).total_seconds() / 3600

    state = {}
    if db is not None:
        try:
            from app.models.profile import Profile

            profile = db.query(Profile).first()
            state = ((profile.data if profile else {}) or {}).get(STATE_KEY) or {}
        except Exception as exc:
            logger.warning("backups: could not read the stored state: %s", exc)

    interval = max(1, int(getattr(settings, "BACKUP_INTERVAL_HOURS", 24)))
    return {
        "enabled": enabled(),
        "directory": str(directory()),
        "count": len(files),
        "keep": _keep(),
        "newest": newest.name if newest else None,
        "newest_bytes": newest.stat().st_size if newest else None,
        "age_hours": round(age_hours, 1) if age_hours is not None else None,
        # The judgement the page needs to make. Twice the interval, because one
        # missed run is a restart and two is a job that has stopped.
        "stale": age_hours is None or age_hours > interval * 2,
        "interval_hours": interval,
        "last_error": state.get("last_error"),
        "last_attempt": state.get("last_attempt"),
        "last_success": state.get("last_success"),
        "total_bytes": sum(path.stat().st_size for path in files),
    }


def restore_command(name: str | None = None) -> str:
    """
    The command that actually restores one of these.

    Written down here, and shown on the page, because a restore procedure
    nobody has read is the same as not having one — and it will be needed on
    the worst day, from memory, under time pressure.
    """
    filename = name or (existing()[0].name if existing() else f"{FILE_PREFIX}....sql.gz")
    # Run from `web`, not from `postgres`: the backups live on the storage
    # volume, which only the app containers mount, and `web` now carries psql
    # for exactly this reason. `$DATABASE_URL` is already in its environment,
    # so the credentials are not typed on the worst day of the year.
    #
    # ON_ERROR_STOP is not optional. Without it psql reports success after
    # skipping every statement it could not run, so a restore that recreated
    # nothing exits 0 and looks like it worked — which is the same failure as
    # an unverified backup, arriving one step later.
    return (
        "docker compose -f docker-compose.prod.yml exec -T web "
        f"sh -c 'gunzip -c {directory()}/{filename} "
        "| psql -v ON_ERROR_STOP=1 \"$DATABASE_URL\"'"
    )
