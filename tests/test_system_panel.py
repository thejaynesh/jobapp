"""
The system panel on /runs.

Everything it shows previously needed a container shell to inspect, which meant
nobody inspected it. So what these tests defend is mostly that the panel keeps
rendering: a status display that disappears when a subsystem is unhealthy is
worse than none, because the moment you need it is exactly the moment it breaks.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.models.browser_task import BrowserTask
from app.models.profile import Profile
from app.services import browser_tasks, interview_corpus


@pytest.fixture
def agent_token(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_TOKEN", "a-token")


class TestPanelRenders:
    def test_the_runs_page_includes_it(self, client):
        body = client.get("/runs").text
        assert "System" in body
        assert "Browser agent" in body
        assert "Interview corpus" in body

    def test_it_renders_on_an_empty_database(self, client):
        # First load after a deploy, with nothing to show.
        assert client.get("/runs").status_code == 200

    def test_the_partial_can_be_fetched_alone(self, client):
        response = client.get("/runs/system")
        assert response.status_code == 200
        assert "system-panel" in response.text


class TestAgentSection:
    def test_says_when_no_token_is_configured(self, client, monkeypatch):
        monkeypatch.setattr(settings, "AGENT_TOKEN", "")
        assert "AGENT_TOKEN" in client.get("/runs/system").text

    def test_shows_queue_depth(self, client, db, agent_token):
        browser_tasks.enqueue(db, "ping")
        browser_tasks.enqueue(db, "ping")
        assert "Waiting" in client.get("/runs/system").text

    def test_distinguishes_never_claimed_from_nobody_running(self, client, db, agent_token):
        # An agent polling an empty queue leaves no trace, so "no task has been
        # claimed" must not be presented as "no agent exists".
        browser_tasks.enqueue(db, "ping")
        assert "leaves no trace" in client.get("/runs/system").text

    def test_names_the_agent_that_last_claimed_work(self, client, db, agent_token):
        browser_tasks.enqueue(db, "ping")
        browser_tasks.lease(db, agent_id="extension-abc123")
        assert "extension-abc123" in client.get("/runs/system").text

    def test_lists_recent_tasks_with_their_status(self, client, db, agent_token):
        task = browser_tasks.enqueue(db, "resolve_link", {"url": "https://x/1"})
        browser_tasks.lease(db, agent_id="ext")
        browser_tasks.complete(db, task.id, {"final_url": "https://y/1"}, agent_id="ext")
        body = client.get("/runs/system").text
        assert "resolve_link" in body
        assert "done" in body


class TestPing:
    def test_the_button_queues_a_task(self, client, db, agent_token):
        client.post("/runs/agent/ping")
        task = db.query(BrowserTask).filter(BrowserTask.kind == "ping").first()
        assert task is not None
        assert task.status == "queued"

    def test_it_returns_the_refreshed_panel(self, client, agent_token):
        response = client.post("/runs/agent/ping")
        assert response.status_code == 200
        assert "system-panel" in response.text


class TestMailboxSection:
    def test_says_why_polling_is_off(self, client, monkeypatch):
        monkeypatch.setattr(settings, "IMAP_ENABLED", False)
        assert "IMAP_ENABLED" in client.get("/runs/system").text

    def test_reports_the_last_poll_when_there_has_been_one(self, client, db, monkeypatch):
        monkeypatch.setattr(settings, "IMAP_ENABLED", True)
        monkeypatch.setattr(settings, "IMAP_HOST", "imap.gmail.com")
        monkeypatch.setattr(settings, "IMAP_USERNAME", "me@example.com")
        monkeypatch.setattr(settings, "IMAP_PASSWORD", "pw")
        db.add(Profile(data={"mailbox": {
            "last_poll": "2026-08-12T09:30:00+00:00",
            "last_counts": {"scanned": 12, "replies": 1, "bounces": 0, "skipped": 2},
        }}))
        db.commit()
        body = client.get("/runs/system").text
        assert "2026-08-12 09:30" in body
        assert "Replies" in body

    def test_says_when_configured_but_not_yet_run(self, client, db, monkeypatch):
        monkeypatch.setattr(settings, "IMAP_ENABLED", True)
        monkeypatch.setattr(settings, "IMAP_HOST", "imap.gmail.com")
        monkeypatch.setattr(settings, "IMAP_USERNAME", "me@example.com")
        monkeypatch.setattr(settings, "IMAP_PASSWORD", "pw")
        db.add(Profile(data={}))
        db.commit()
        assert "has not run yet" in client.get("/runs/system").text


class TestCorpusSection:
    def test_shows_totals(self, client, db):
        interview_corpus.ingest(db, [{
            "company": "Acme", "source": "reddit", "url": "https://x/1",
            "title": "t", "body": "b" * 300,
            "posted_at": datetime.now(timezone.utc) - timedelta(days=10),
        }])
        assert "Reports" in client.get("/runs/system").text

    def test_every_source_is_listed_even_at_zero(self, client, db):
        # A source at zero is the thing worth noticing; omitting it is how it
        # goes unnoticed.
        body = client.get("/runs/system").text
        for source in ("reddit", "github", "geeksforgeeks"):
            assert source in body


class TestDegradation:
    def test_a_failing_subsystem_does_not_take_the_page_down(self, client, monkeypatch):
        # The moment a status panel is worth reading is the moment something is
        # wrong, so it must not be the thing that breaks.
        from app.services import browser_tasks as bt

        def explode(db):
            raise RuntimeError("queue is unreachable")

        monkeypatch.setattr(bt, "queue_stats", explode)
        response = client.get("/runs")
        assert response.status_code == 200
        assert "agent queue" in response.text
