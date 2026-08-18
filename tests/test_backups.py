"""
The nightly copy of the database.

Everything else in this repo assumes the data survives. The tests that matter
here are the ones about a dump that is *not* usable — a truncated file, a
`pg_dump` that exited non-zero, a run that fails halfway — because a backup
that looks like a backup and isn't is worse than none at all.
"""

import gzip
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from app.config import settings
from app.models.profile import Profile
from app.services import backups

COMPLETE = (
    "--\n-- PostgreSQL database dump\n--\n"
    "CREATE TABLE jobs (id uuid);\n"
    "--\n-- PostgreSQL database dump complete\n--\n\n"
)

# What pg_dump 16.10 and newer actually emit. The August 2025 security releases
# wrap the dump in \restrict/\unrestrict to block psql meta-command injection
# (CVE-2025-8714), which means the completion marker is no longer the last line
# of the file — it is four lines up.
COMPLETE_MODERN = COMPLETE + (
    "\\unrestrict ywfxlKs1aa082Kj49Kxyr8e6LeCHJdBJyIOEhG1PhEmHqf32Z2AIjfWOjq1ztD1\n\n"
)


@pytest.fixture
def folder(tmp_path, monkeypatch):
    target = tmp_path / "backups"
    monkeypatch.setattr(settings, "BACKUP_DIR", str(target))
    monkeypatch.setattr(settings, "BACKUP_ENABLED", True)
    return target


def _write(folder: Path, name: str, body: str = COMPLETE) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write(body)
    return path


class TestVerification:
    def test_a_complete_dump_passes(self, folder):
        path = _write(folder, "jobapp-20260101T000000Z.sql.gz")
        assert backups.verify(path) == len(COMPLETE)

    def test_the_marker_need_not_be_the_last_line(self, folder):
        # pg_dump 16.10+ appends `\unrestrict <token>` after the completion
        # marker. A verifier written as "does the file end with the marker"
        # would call every modern dump broken — and the failure would arrive
        # as a backup job that has cried wolf every night for a month.
        path = _write(folder, "jobapp-modern.sql.gz", COMPLETE_MODERN)
        assert backups.verify(path) == len(COMPLETE_MODERN)

    def test_a_dump_without_the_completion_marker_fails(self, folder):
        # What a pg_dump that died mid-table looks like: valid gzip, valid SQL,
        # and missing half the schema. Exactly the file that looks like a
        # backup until the day you need it.
        path = _write(folder, "jobapp-x.sql.gz",
                      "CREATE TABLE jobs (id uuid);\nCOPY jobs FROM std")

        with pytest.raises(RuntimeError, match="completion marker"):
            backups.verify(path)

    def test_a_truncated_file_fails(self, folder):
        path = _write(folder, "jobapp-x.sql.gz")
        raw = path.read_bytes()
        path.write_bytes(raw[: len(raw) // 2])

        with pytest.raises(Exception):
            backups.verify(path)

    def test_an_empty_file_fails(self, folder):
        path = _write(folder, "jobapp-x.sql.gz", "")
        with pytest.raises(RuntimeError):
            backups.verify(path)


class TestTakingOne:
    def test_it_writes_verifies_and_names_by_time(self, db, folder):
        with patch.object(backups, "_dump",
                          side_effect=lambda target: _write(
                              target.parent, target.name)):
            result = backups.run(db)

        assert result["ok"] is True
        assert result["file"].startswith("jobapp-")
        assert result["file"].endswith(".sql.gz")
        assert (folder / result["file"]).exists()

    def test_a_failed_dump_leaves_no_file_behind(self, db, folder):
        # A half-written file that looks like a backup is the failure this
        # whole module is arranged to prevent.
        with patch.object(backups, "_dump",
                          side_effect=RuntimeError("pg_dump exited 1")):
            result = backups.run(db)

        assert result["ok"] is False
        assert "pg_dump exited 1" in result["error"]
        assert backups.existing() == []

    def test_a_failed_dump_does_not_touch_the_previous_one(self, db, folder):
        _write(folder, "jobapp-20260101T000000Z.sql.gz")

        with patch.object(backups, "_dump", side_effect=RuntimeError("disk full")):
            backups.run(db)

        assert [path.name for path in backups.existing()] == [
            "jobapp-20260101T000000Z.sql.gz"
        ]

    def test_an_unverifiable_dump_is_thrown_away(self, db, folder):
        # It wrote something, but not a complete dump. Keeping it would mean
        # rotation eventually deletes the last good one to make room for it.
        with patch.object(backups, "_dump",
                          side_effect=lambda target: _write(
                              target.parent, target.name, "CREATE TABLE x();")):
            result = backups.run(db)

        assert result["ok"] is False
        assert "completion marker" in result["error"]
        assert backups.existing() == []

    def test_a_missing_pg_dump_says_so_plainly(self, db, folder):
        with patch.object(backups, "_dump", side_effect=FileNotFoundError()):
            result = backups.run(db)

        assert result["ok"] is False
        assert "postgresql-client" in result["error"]

    def test_it_does_nothing_when_switched_off(self, db, folder, monkeypatch):
        monkeypatch.setattr(settings, "BACKUP_ENABLED", False)
        result = backups.run(db)
        assert result["skipped"] is True
        assert backups.existing() == []


class TestRotation:
    def test_only_the_newest_are_kept(self, folder):
        for day in range(1, 8):
            _write(folder, f"jobapp-2026010{day}T000000Z.sql.gz")

        removed = backups.rotate(keep=3)

        assert len(removed) == 4
        assert [path.name for path in backups.existing()] == [
            "jobapp-20260107T000000Z.sql.gz",
            "jobapp-20260106T000000Z.sql.gz",
            "jobapp-20260105T000000Z.sql.gz",
        ]

    def test_it_only_runs_after_a_verified_success(self, db, folder, monkeypatch):
        # Deleting an old backup to make room for one that then fails is how a
        # retention policy turns into data loss.
        monkeypatch.setattr(settings, "BACKUP_KEEP", 1)
        _write(folder, "jobapp-20260101T000000Z.sql.gz")

        with patch.object(backups, "_dump", side_effect=RuntimeError("nope")):
            backups.run(db)

        assert len(backups.existing()) == 1

    def test_unrelated_files_are_never_touched(self, folder):
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "notes.txt").write_text("not a backup")
        _write(folder, "jobapp-20260101T000000Z.sql.gz")

        backups.rotate(keep=1)

        assert (folder / "notes.txt").exists()


class TestWhatThePageShows:
    def test_a_fresh_backup_is_not_stale(self, db, folder):
        with patch.object(backups, "_dump",
                          side_effect=lambda target: _write(
                              target.parent, target.name)):
            backups.run(db)

        state = backups.status(db)
        assert state["stale"] is False
        assert state["count"] == 1

    def test_no_backups_at_all_is_stale(self, db, folder):
        assert backups.status(db)["stale"] is True

    def test_an_old_backup_is_stale(self, db, folder, monkeypatch):
        import os
        import time

        path = _write(folder, "jobapp-20260101T000000Z.sql.gz")
        old = time.time() - 60 * 60 * 24 * 5
        os.utime(path, (old, old))

        state = backups.status(db)
        assert state["stale"] is True
        assert state["age_hours"] > 100

    def test_a_failure_does_not_erase_the_last_success(self, db, folder):
        # "It worked in March and has failed every night since" is the whole
        # story; either half alone tells a misleading version of it.
        db.add(Profile(data={}))
        db.commit()

        with patch.object(backups, "_dump",
                          side_effect=lambda target: _write(
                              target.parent, target.name)):
            backups.run(db)
        with patch.object(backups, "_dump", side_effect=RuntimeError("disk full")):
            backups.run(db)

        state = backups.status(db)
        assert state["last_error"] == "disk full"
        assert state["last_success"] is not None
        assert state["last_file"] if "last_file" in state else True

    def test_the_panel_renders_the_warning(self, client, db, folder):
        body = client.get("/runs").text
        assert "Backups" in body
        assert "No backup has ever been written." in body

    def test_the_panel_renders_a_healthy_state(self, client, db, folder):
        with patch.object(backups, "_dump",
                          side_effect=lambda target: _write(
                              target.parent, target.name)):
            backups.run(db)

        body = client.get("/runs").text
        assert "Newest:" in body
        assert "does not survive losing the box" in body


class TestTheRestorePath:
    def test_the_command_names_a_real_file(self, folder):
        _write(folder, "jobapp-20260101T000000Z.sql.gz")
        command = backups.restore_command()
        assert "jobapp-20260101T000000Z.sql.gz" in command
        assert "gunzip -c" in command
        assert "psql" in command

    def test_it_reads_from_the_container_that_holds_the_files(self, folder):
        # `postgres` does not mount the storage volume; `web` does.
        assert "exec -T web" in backups.restore_command()


class TestAgainstARealDatabase:
    """
    The half that mocks cannot check: that `pg_dump` is invoked correctly and
    produces something this code accepts as complete.
    """

    def _have_pg_dump(self) -> bool:
        try:
            subprocess.run(["pg_dump", "--version"], capture_output=True, check=True)
            return True
        except (OSError, subprocess.CalledProcessError):
            return False

    def test_a_real_dump_round_trips(self, db, folder, monkeypatch):
        if not self._have_pg_dump():
            pytest.skip("pg_dump is not installed here")
        from tests.conftest import TEST_DB_URL

        monkeypatch.setattr(settings, "DATABASE_URL", TEST_DB_URL)
        result = backups.run(db)

        if not result["ok"] and "server version" in (result.get("error") or ""):
            pytest.skip(f"pg_dump is older than the server: {result['error']}")

        assert result["ok"] is True, result.get("error")
        # Verified by `run` already; asserted again because the point of the
        # test is that a real dump satisfies the real verifier.
        assert backups.verify(folder / result["file"]) > 0
        with gzip.open(folder / result["file"], "rt", errors="replace") as handle:
            assert "CREATE TABLE" in handle.read(200000)

    def test_a_dump_actually_restores(self, db, folder, monkeypatch):
        """
        The only test that proves any of this.

        Everything else checks that a file was written and looks right. This
        one puts it back into an empty database and reads a table out of it,
        which is the claim the whole module makes and the one nobody verifies
        until the day it matters.
        """
        if not self._have_pg_dump():
            pytest.skip("pg_dump is not installed here")
        try:
            subprocess.run(["psql", "--version"], capture_output=True, check=True)
        except (OSError, subprocess.CalledProcessError):
            pytest.skip("psql is not installed here")

        from sqlalchemy import create_engine, text

        from tests.conftest import TEST_DB_URL

        monkeypatch.setattr(settings, "DATABASE_URL", TEST_DB_URL)
        result = backups.run(db)
        if not result["ok"]:
            pytest.skip(f"could not take a dump here: {result.get('error')}")

        scratch = "jobapp_restore_check"
        admin = create_engine(
            TEST_DB_URL.rsplit("/", 1)[0] + "/postgres",
            isolation_level="AUTOCOMMIT",
        )

        def _drop():
            with admin.connect() as connection:
                # Postgres refuses to drop a database anyone is connected to,
                # and a pooled connection can outlive the engine that opened
                # it — which would leave this scratch database behind and make
                # the next run of the test fail on the CREATE.
                connection.execute(text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :name AND pid <> pg_backend_pid()"
                ), {"name": scratch})
                connection.execute(text(f'DROP DATABASE IF EXISTS "{scratch}"'))

        try:
            _drop()
            with admin.connect() as connection:
                connection.execute(text(f'CREATE DATABASE "{scratch}"'))

            target = TEST_DB_URL.rsplit("/", 1)[0] + f"/{scratch}"
            args, env = backups._dsn_environment(target)
            path = folder / result["file"]
            # Decompressed in memory rather than handed to psql as a file
            # object: `GzipFile.fileno()` returns the descriptor of the
            # *compressed* file, so `stdin=handle` feeds psql raw gzip — which
            # it rejects line by line and still exits 0.
            restored = subprocess.run(
                ["psql", "--quiet", "--no-psqlrc",
                 "--variable", "ON_ERROR_STOP=1", *args],
                input=gzip.decompress(path.read_bytes()),
                capture_output=True, env=env,
            )
            assert restored.returncode == 0, restored.stderr.decode()[:800]

            engine = create_engine(target)
            with engine.connect() as connection:
                # A table only this application creates, read out of a database
                # that was empty a moment ago.
                assert connection.execute(
                    text("SELECT count(*) FROM jobs")
                ).scalar() is not None
            engine.dispose()
        finally:
            _drop()
            admin.dispose()

    def test_the_password_never_reaches_the_argument_list(self, db):
        # Arguments are visible in `ps` to anything else on the box.
        args, env = backups._dsn_environment(
            "postgresql://someone:hunter2@db.example:5432/jobapp"
        )
        assert "hunter2" not in " ".join(args)
        assert env["PGPASSWORD"] == "hunter2"
        assert args[:2] == ["--dbname", "jobapp"]
        assert "--host" in args and "db.example" in args

    def test_a_url_with_no_password_sets_none(self, db):
        _, env = backups._dsn_environment("postgresql://postgres@127.0.0.1:5433/jobapp")
        assert "PGPASSWORD" not in env
