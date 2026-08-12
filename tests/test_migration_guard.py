"""
Refusing to serve against a schema that failed to migrate.

The failure this prevents is a quiet one: new code runs against an old schema
and the first symptom is an UndefinedTable traceback from whichever feature
happens to touch the missing table first, which reads as a bug in that feature
rather than as an unapplied migration.
"""

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
