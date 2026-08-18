"""
What the browser extension actually did.

Every question about this subsystem started the same way for months: is it even
installed? A harvest that found nothing, an autofill that recognised two fields
out of fifteen, an overlay lookup on a site the URL matcher cannot resolve — all
of them happen on someone else's page and left no trace, and that silence is
indistinguishable from an extension nobody reinstalled after a browser update.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.models.agent_event import AgentEvent
from app.models.browser_task import BrowserTask
from app.services import agent_events, browser_tasks

TOKEN = "test-agent-token-value"


@pytest.fixture
def agent(client, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_ENABLED", True)
    monkeypatch.setattr(settings, "AGENT_TOKEN", TOKEN)
    monkeypatch.setattr(settings, "APP_PASSWORD", "irrelevant-but-required")
    monkeypatch.setattr(settings, "SECRET_KEY", "not-the-placeholder-value")
    return client


def auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def _event(db, kind="harvest", **kwargs):
    row = agent_events.record(db, kind, **kwargs)
    db.commit()
    return row


class TestWhatGetsStored:
    def test_a_url_becomes_a_host(self, db):
        # The URL is the posting, which is already in `jobs`. The host is what
        # you group by — and it keeps this from being a browsing history.
        row = _event(db, url="https://www.linkedin.com/jobs/view/4242?ref=abc")
        assert row.host == "www.linkedin.com"

    def test_a_url_that_says_nothing_stores_no_host(self, db):
        assert _event(db, url="not a url").host is None
        assert _event(db, url="").host is None

    def test_an_unknown_kind_is_filed_rather_than_rejected(self, db):
        # An extension newer than the server still leaves a trace.
        assert _event(db, kind="teleported").kind == "other"

    def test_a_summary_is_clipped(self, db):
        row = _event(db, summary={"note": "x" * 5000, "count": 3, "ok": True})
        assert len(row.summary["note"]) == 300
        assert row.summary["count"] == 3
        assert row.summary["ok"] is True

    def test_an_empty_summary_is_null(self, db):
        assert _event(db, summary={}).summary is None
        assert _event(db, summary="nonsense").summary is None

    def test_it_never_raises(self, db):
        # Recording that a harvest happened must not be able to break it.
        from unittest.mock import patch

        with patch("app.services.agent_events._clean_summary",
                   side_effect=RuntimeError("boom")):
            assert agent_events.record(db, "harvest", summary={"a": 1}) is None


class TestTheReportEndpoint:
    def test_a_batch_is_stored(self, agent, db):
        body = agent.post("/api/agent/report", headers=auth(), json={
            "agent_id": "laptop",
            "events": [
                {"kind": "autofill", "url": "https://boards.greenhouse.io/x/jobs/1",
                 "ok": True, "summary": {"filled": 6}},
                {"kind": "attach_resume", "url": "https://boards.greenhouse.io/x/jobs/1",
                 "ok": False, "summary": {"reason": "no unambiguous file input"}},
            ],
        }).json()

        assert body == {"stored": 2, "rejected": 0}
        rows = db.query(AgentEvent).order_by(AgentEvent.kind).all()
        assert {row.kind for row in rows} == {"attach_resume", "autofill"}
        assert all(row.agent_id == "laptop" for row in rows)

    def test_a_single_bare_event_is_accepted(self, agent, db):
        # One less shape for the client to get wrong.
        body = agent.post("/api/agent/report", headers=auth(), json={
            "kind": "overlay_open", "url": "https://jobs.lever.co/x/1",
        }).json()
        assert body["stored"] == 1

    def test_a_malformed_entry_costs_that_entry(self, agent, db):
        body = agent.post("/api/agent/report", headers=auth(), json={
            "events": ["not a dict", {"kind": "poll"}],
        }).json()
        assert body["stored"] == 1
        assert body["rejected"] == 1

    def test_an_oversized_batch_is_capped(self, agent, db):
        events = [{"kind": "poll"} for _ in range(agent_events.MAX_BATCH + 10)]
        body = agent.post(
            "/api/agent/report", headers=auth(), json={"events": events}
        ).json()
        assert body["stored"] == agent_events.MAX_BATCH
        assert body["rejected"] == 10

    def test_it_needs_the_token(self, agent, db):
        assert agent.post("/api/agent/report", json={"events": []}).status_code == 401


class TestHarvestLeavesATrace:
    def test_a_harvest_that_found_nothing_is_still_recorded(self, agent, db):
        # The commonest outcome, and previously invisible — so "the interceptor
        # is forwarding rubbish" and "the extension is not running" looked the
        # same from here.
        agent.post("/api/agent/harvest", headers=auth(), json={
            "payload": {"nothing": "job-shaped"},
            "source_url": "https://www.linkedin.com/jobs/search",
            "agent_id": "laptop",
        })

        row = db.query(AgentEvent).filter(AgentEvent.kind == "harvest").one()
        assert row.host == "www.linkedin.com"
        assert row.summary["found"] == 0
        assert row.agent_id == "laptop"


class TestTasksLeaveATrace:
    def _task(self, db, kind="resolve_link"):
        task = browser_tasks.enqueue(
            db, kind, {"url": "https://www.jooble.org/away/1"}
        )
        leased = browser_tasks.lease(db, [kind], agent_id="laptop", limit=1)
        assert leased
        return task

    def test_a_completed_task_is_recorded(self, db):
        task = self._task(db)
        browser_tasks.complete(
            db, task.id, {"final_url": "https://boards.greenhouse.io/x/jobs/1"},
            agent_id="laptop",
        )

        row = db.query(AgentEvent).filter(AgentEvent.kind == "task_done").one()
        assert row.ok is True
        assert row.summary["task_kind"] == "resolve_link"
        assert row.host == "www.jooble.org"

    def test_a_retry_is_not_a_failure(self, db):
        # A requeued attempt is the retry working. Counting each one would make
        # every transient hiccup look like a broken host.
        task = self._task(db)
        browser_tasks.fail(db, task.id, "timed out", agent_id="laptop")

        assert db.query(AgentEvent).filter(
            AgentEvent.kind == "task_failed"
        ).count() == 0

    def test_giving_up_is(self, db):
        task = self._task(db)
        browser_tasks.fail(db, task.id, "403 forever", agent_id="laptop",
                           permanent=True)

        row = db.query(AgentEvent).filter(AgentEvent.kind == "task_failed").one()
        assert row.ok is False
        assert "403" in row.summary["error"]


class TestReadingItBack:
    def _seed(self, db):
        _event(db, kind="harvest", url="https://www.linkedin.com/x",
               summary={"found": 25, "inserted": 4})
        _event(db, kind="harvest", url="https://www.linkedin.com/x",
               summary={"found": 10, "inserted": 0})
        _event(db, kind="autofill", url="https://myworkdayjobs.com/x", ok=False)
        _event(db, kind="autofill", url="https://myworkdayjobs.com/x", ok=False)
        _event(db, kind="autofill", url="https://boards.greenhouse.io/x")

    def test_counts_by_kind_with_failures_separated(self, db):
        self._seed(db)
        result = agent_events.summary(db)

        by_kind = {row["kind"]: row for row in result["by_kind"]}
        assert by_kind["autofill"]["count"] == 3
        assert by_kind["autofill"]["failed"] == 2
        assert by_kind["harvest"]["failed"] == 0

    def test_the_failing_hosts_are_named(self, db):
        # The most useful view here: a host that fails every time needs a
        # different approach, not a shrug about flakiness.
        self._seed(db)
        hosts = agent_events.summary(db)["failing_hosts"]
        assert hosts[0] == {"host": "myworkdayjobs.com", "count": 2}

    def test_harvest_yield_is_the_claim_as_a_number(self, db):
        self._seed(db)
        assert agent_events.harvest_yield(db) == {
            "posts": 2, "found": 35, "inserted": 4,
        }

    def test_events_outside_the_window_are_excluded(self, db):
        old = _event(db, kind="harvest", summary={"found": 99, "inserted": 99})
        old.created_at = datetime.now(timezone.utc) - timedelta(days=30)
        db.commit()

        assert agent_events.summary(db, days=7)["total"] == 0

    def test_the_panel_renders(self, client, db):
        self._seed(db)
        body = client.get("/runs").text
        assert "Browser agent" in body
        assert "myworkdayjobs.com" in body

    def test_the_panel_says_so_when_nothing_reported(self, client, db):
        body = client.get("/runs").text
        assert "Nothing reported in" in body


class TestEveryBrowserIsRemembered:
    def test_two_browsers_do_not_overwrite_each_other(self, db):
        # A single slot meant a laptop and a desktop overwrote each other, so
        # "nothing has polled since Tuesday" got reported about whichever
        # happened to be second.
        from app.models.profile import Profile

        db.add(Profile(data={}))
        db.commit()

        browser_tasks.record_agent_seen(db, "laptop", ["resolve_link"])
        browser_tasks.record_agent_seen(db, "desktop", ["fetch_json"])

        names = [entry["agent_id"] for entry in browser_tasks.known_agents(db)]
        assert set(names) == {"laptop", "desktop"}

    def test_the_most_recent_is_still_reported_as_last(self, db):
        from app.models.profile import Profile

        db.add(Profile(data={}))
        db.commit()

        browser_tasks.record_agent_seen(db, "laptop", [])
        browser_tasks.record_agent_seen(db, "desktop", [])

        assert browser_tasks.last_agent(db)["agent_id"] == "desktop"

    def test_the_map_is_capped(self, db):
        # An agent_id regenerated per browser session would otherwise grow the
        # profile blob forever.
        from app.models.profile import Profile

        db.add(Profile(data={}))
        db.commit()
        for n in range(20):
            browser_tasks.record_agent_seen(db, f"agent-{n}", [])

        assert len(browser_tasks.known_agents(db)) == 12


class TestPruning:
    def test_events_beyond_the_cap_go(self, db):
        for n in range(10):
            row = _event(db, kind="poll")
            row.created_at = datetime.now(timezone.utc) - timedelta(minutes=10 - n)
        db.commit()

        assert agent_events.prune(db, keep=4) == 6
        assert db.query(AgentEvent).count() == 4

    def test_a_table_under_the_cap_is_untouched(self, db):
        _event(db, kind="poll")
        assert agent_events.prune(db, keep=100) == 0

    def test_finished_tasks_older_than_the_window_go(self, db):
        task = browser_tasks.enqueue(db, "ping", {})
        task.status = "done"
        task.completed_at = datetime.now(timezone.utc) - timedelta(days=30)
        db.commit()

        assert browser_tasks.prune(db, days=14) == 1

    def test_queued_work_is_never_pruned(self, db):
        # A queued task is not old, it is late. Deleting it would silently drop
        # work nobody has done yet.
        browser_tasks.enqueue(db, "ping", {})
        assert browser_tasks.prune(db, days=1) == 0
        assert db.query(BrowserTask).count() == 1

    def test_a_recently_finished_task_stays(self, db):
        task = browser_tasks.enqueue(db, "ping", {})
        task.status = "done"
        task.completed_at = datetime.now(timezone.utc) - timedelta(days=1)
        db.commit()

        assert browser_tasks.prune(db, days=14) == 0
