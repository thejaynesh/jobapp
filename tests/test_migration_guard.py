"""
Refusing to serve against a schema that failed to migrate.

The failure this prevents is a quiet one: new code runs against an old schema
and the first symptom is an UndefinedTable traceback from whichever feature
happens to touch the missing table first, which reads as a bug in that feature
rather than as an unapplied migration.
"""

from unittest.mock import MagicMock, patch

import pytest

import app.main as main


@pytest.fixture
def failed_migration(monkeypatch):
    monkeypatch.setattr(
        main, "_migration_failure", 'relation "browser_tasks" does not exist'
    )


class TestRefusesToServe:
    def test_pages_are_refused(self, client, failed_migration):
        response = client.get("/jobs")
        assert response.status_code == 503

    def test_the_reason_is_in_the_response(self, client, failed_migration):
        # Whoever sees this has to fix it, so it carries alembic's own words
        # rather than a generic "service unavailable".
        body = client.get("/jobs").json()["detail"]
        assert "schema is not up to date" in body
        assert "browser_tasks" in body

    def test_the_agent_api_is_refused_too(self, client, failed_migration):
        assert client.get("/api/agent/hello").status_code == 503

    def test_login_is_refused(self, client, failed_migration):
        # Otherwise you log in successfully and every page behind it fails,
        # which looks like a broken app rather than a broken deploy.
        assert client.get("/login").status_code == 503


class TestHealthStaysUp:
    def test_health_still_answers(self, client, failed_migration):
        # A restart cannot fix a migration that will not apply, so failing the
        # container health check would only produce a restart loop.
        assert client.get("/health").status_code == 200


class TestOnlyOneWorkerMigrates:
    """
    `uvicorn --workers 2` forks two processes and each runs the lifespan, so
    both shelled out to `alembic upgrade head` against the same database at
    once. One commits; the other fails on an object that now exists, records
    the failure, and answers 503 to every request it receives for the life of
    the process — `_migration_failure` is module state that only another
    lifespan clears, so it never heals, and both workers share the listening
    socket.

    Reproduced against an empty database before the fix: of four concurrent
    workers, **one** came up able to serve. With the advisory lock, four.
    """

    def _run(self, returncode=0, stdout="up to date", stderr=""):
        conn = MagicMock()
        engine = MagicMock()
        engine.connect.return_value = conn
        proc = MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)
        with patch("app.database.engine", engine), \
             patch.object(main.subprocess, "run", return_value=proc) as run:
            failure = main._migrate_under_lock()
        sql = " ".join(str(c.args[0]) for c in conn.exec_driver_sql.call_args_list)
        return failure, sql, conn, run

    def test_the_migration_is_serialised_on_an_advisory_lock(self):
        failure, sql, _, run = self._run()
        assert failure is None
        assert "pg_advisory_lock" in sql
        run.assert_called_once()

    def test_the_lock_is_released_afterwards(self):
        _, sql, conn, _ = self._run()
        assert "pg_advisory_unlock" in sql
        conn.close.assert_called_once()

    def test_the_lock_is_released_even_when_the_migration_fails(self):
        failure, sql, conn, _ = self._run(returncode=1, stderr="boom", stdout="")
        assert failure == "boom"
        assert "pg_advisory_unlock" in sql
        conn.close.assert_called_once()

    def test_it_never_migrates_without_the_lock(self):
        """
        A database we cannot reach is reported, not worked around. Migrating
        unlocked here is exactly the race the lock exists to remove.
        """
        engine = MagicMock()
        engine.connect.side_effect = RuntimeError("no route to host")
        with patch("app.database.engine", engine), \
             patch.object(main.subprocess, "run") as run:
            failure = main._migrate_under_lock()
        run.assert_not_called()
        assert "no route to host" in failure

    def test_the_lock_id_is_a_valid_postgres_advisory_key(self):
        # pg_advisory_lock(bigint) is fine, but keeping it inside int4 avoids
        # any ambiguity about which overload is being called.
        assert 0 < main._MIGRATION_LOCK_ID < 2 ** 31


class TestNormalOperation:
    def test_nothing_is_refused_when_migrations_applied(self, client, monkeypatch):
        monkeypatch.setattr(main, "_migration_failure", None)
        assert client.get("/health").status_code == 200
        assert client.get("/jobs").status_code == 200

    def test_the_accessor_reports_current_state(self, monkeypatch):
        monkeypatch.setattr(main, "_migration_failure", None)
        assert main.migration_failure() is None
        monkeypatch.setattr(main, "_migration_failure", "boom")
        assert main.migration_failure() == "boom"
