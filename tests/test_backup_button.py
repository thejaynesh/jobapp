"""
Back up now, download a backup, and the Backups settings.

The backup ran nightly and could only be taken by hand from a container shell.
Now it is a button, the files are downloadable (the only off-box copy this
system has), and how often and how many are settings — each tested by storing
an override on the profile and checking the behaviour moves.
"""

import gzip
import os
import time
from unittest.mock import patch

import pytest

from app.config import settings
from app.models.profile import Profile
from app.services import backups, tunables


@pytest.fixture
def folder(tmp_path, monkeypatch):
    target = tmp_path / "backups"
    target.mkdir()
    monkeypatch.setattr(settings, "BACKUP_DIR", str(target))
    monkeypatch.setattr(settings, "BACKUP_ENABLED", True)
    return target


def _file(folder, stamp, age_hours=0.0):
    path = folder / f"jobapp-{stamp}.sql.gz"
    with gzip.open(path, "wt") as handle:
        handle.write("-- PostgreSQL database dump complete\n")
    moment = time.time() - age_hours * 3600
    os.utime(path, (moment, moment))
    return path


@pytest.fixture(autouse=True)
def _settings_read_from_the_test_db(db):
    """`tunables.current` opens its own session; point it at the test one."""

    class _Shared:
        def __getattr__(self, name):
            return getattr(db, name)

        def close(self):
            pass

    with patch("app.database.SessionLocal", return_value=_Shared()):
        yield


def _override(db, **values):
    db.add(Profile(data={tunables.STORE_KEY: values}))
    db.commit()


class TestSettings:
    def test_how_many_to_keep_is_a_setting(self, db, folder):
        for day in range(1, 6):
            _file(folder, f"2026010{day}T000000Z")
        _override(db, backup_keep=2)
        backups.rotate()
        assert len(backups.existing()) == 2

    def test_the_interval_is_a_setting(self, db, folder):
        _file(folder, "20260101T000000Z", age_hours=5)
        assert not backups.due()  # env default is 24
        _override(db, backup_interval_hours=4)
        assert backups.due()

    def test_switching_off_stops_the_schedule_but_not_the_button(self, db, folder):
        _override(db, backup_enabled=False)
        assert not backups.due()
        with patch("app.services.backups._dump", side_effect=RuntimeError("ran")):
            assert "ran" in backups.run(db, force=True)["error"]

    def test_the_hourly_tick_skips_when_not_due(self, folder):
        from app.tasks.backup import take_backup

        _file(folder, "20260101T000000Z", age_hours=1)
        with patch("app.services.backups.run") as run:
            assert take_backup()["skipped"]
        run.assert_not_called()


class TestTheButton:
    def test_it_queues_a_forced_backup_on_the_interactive_queue(self, client, db, folder):
        db.add(Profile(data={}))
        db.commit()
        with patch("app.tasks.backup.take_backup.apply_async") as queued:
            body = client.post("/runs/backup").text
        assert queued.call_args.kwargs == {"kwargs": {"force": True}, "queue": "interactive"}
        assert "Backup started" in body
        assert backups.status(db)["in_progress"]

    def test_a_backup_can_be_downloaded(self, client, folder):
        path = _file(folder, "20260102T000000Z")
        response = client.get(f"/runs/backup/{path.name}")
        assert response.status_code == 200
        assert response.content == path.read_bytes()

    def test_only_backups_can_be_downloaded(self, client, folder):
        (folder / "secret.txt").write_text("no")
        assert client.get("/runs/backup/secret.txt").status_code == 404
        assert client.get("/runs/backup/..%2F..%2Fetc%2Fpasswd").status_code == 404

    def test_the_panel_lists_downloads(self, client, db, folder):
        _file(folder, "20260103T000000Z")
        body = client.get("/runs/system").text
        assert "Back up now" in body
        assert "/runs/backup/jobapp-20260103T000000Z.sql.gz" in body
