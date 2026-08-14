"""
The agent queue.

The cases worth defending are the ones that only show up with a real agent
attached: two engines polling at once, a laptop that closes mid-task, work that
sat in the queue past the point of being useful.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.models.browser_task import BrowserTask
from app.services import browser_tasks
from app.services.browser_tasks import TaskError


def _now():
    return datetime.now(timezone.utc)


class TestEnqueue:
    def test_queues_a_task(self, db):
        task = browser_tasks.enqueue(db, "ping", {"a": 1})
        assert task.status == "queued"
        assert task.payload == {"a": 1}
        assert task.attempts == 0
        assert task.expires_at > _now()

    def test_unknown_kind_is_refused(self, db):
        with pytest.raises(TaskError) as exc:
            browser_tasks.enqueue(db, "make_coffee")
        # The message names the kinds that do exist, since the caller is code
        # someone is in the middle of writing.
        assert "ping" in str(exc.value)

    def test_payload_defaults_to_empty_rather_than_null(self, db):
        assert browser_tasks.enqueue(db, "ping").payload == {}


class TestLease:
    def test_leases_queued_work(self, db):
        browser_tasks.enqueue(db, "ping")
        leased = browser_tasks.lease(db, agent_id="a1")
        assert len(leased) == 1
        assert leased[0].status == "leased"
        assert leased[0].agent_id == "a1"
        assert leased[0].attempts == 1
        assert leased[0].lease_expires_at > _now()

    def test_empty_queue_is_not_an_error(self, db):
        assert browser_tasks.lease(db, agent_id="a1") == []

    def test_a_leased_task_is_not_leased_again(self, db):
        browser_tasks.enqueue(db, "ping")
        first = browser_tasks.lease(db, agent_id="a1")
        second = browser_tasks.lease(db, agent_id="a2")
        assert len(first) == 1
        assert second == [], "two agents must never hold the same task"

    def test_filters_by_kind(self, db):
        browser_tasks.enqueue(db, "ping")
        leased = browser_tasks.lease(db, ["resolve_link"], agent_id="a1")
        assert leased == []
        assert len(browser_tasks.lease(db, ["ping"], agent_id="a1")) == 1

    def test_unknown_kind_filter_is_refused(self, db):
        with pytest.raises(TaskError):
            browser_tasks.lease(db, ["nonsense"], agent_id="a1")

    def test_higher_priority_goes_first(self, db):
        browser_tasks.enqueue(db, "ping", {"n": "low"})
        browser_tasks.enqueue(db, "ping", {"n": "high"}, priority=10)
        leased = browser_tasks.lease(db, agent_id="a1")
        assert leased[0].payload["n"] == "high"

    def test_same_priority_is_fifo(self, db):
        browser_tasks.enqueue(db, "ping", {"n": "first"})
        browser_tasks.enqueue(db, "ping", {"n": "second"})
        leased = browser_tasks.lease(db, agent_id="a1", limit=2)
        assert [t.payload["n"] for t in leased] == ["first", "second"]

    def test_batch_is_capped(self, db, monkeypatch):
        monkeypatch.setattr(settings, "AGENT_MAX_LEASE_BATCH", 2)
        for _ in range(5):
            browser_tasks.enqueue(db, "ping")
        assert len(browser_tasks.lease(db, agent_id="a1", limit=99)) == 2

    def test_expired_work_is_never_leased(self, db):
        task = browser_tasks.enqueue(db, "ping")
        task.expires_at = _now() - timedelta(seconds=1)
        db.commit()
        assert browser_tasks.lease(db, agent_id="a1") == []


class TestReap:
    def test_a_lapsed_lease_returns_to_the_queue(self, db):
        browser_tasks.enqueue(db, "ping")
        leased = browser_tasks.lease(db, agent_id="gone")[0]
        leased.lease_expires_at = _now() - timedelta(seconds=1)
        db.commit()

        recovered, _ = browser_tasks.reap(db)
        db.refresh(leased)
        assert recovered == 1
        assert leased.status == "queued"
        assert leased.agent_id is None

    def test_a_lapsed_lease_does_not_count_as_an_attempt(self, db):
        # A closed laptop is not a failed attempt. Counting it would burn
        # through max_attempts on a task that never actually ran.
        browser_tasks.enqueue(db, "ping", max_attempts=2)
        leased = browser_tasks.lease(db, agent_id="gone")[0]
        assert leased.attempts == 1
        leased.lease_expires_at = _now() - timedelta(seconds=1)
        db.commit()

        browser_tasks.reap(db)
        db.refresh(leased)
        assert leased.attempts == 1

    def test_recovered_work_can_be_leased_by_someone_else(self, db):
        browser_tasks.enqueue(db, "ping")
        leased = browser_tasks.lease(db, agent_id="gone")[0]
        leased.lease_expires_at = _now() - timedelta(seconds=1)
        db.commit()

        # reap runs at the top of lease, so the next agent to ask gets it.
        assert len(browser_tasks.lease(db, agent_id="awake")) == 1

    def test_stale_work_expires(self, db):
        task = browser_tasks.enqueue(db, "ping")
        task.expires_at = _now() - timedelta(seconds=1)
        db.commit()

        _, expired = browser_tasks.reap(db)
        db.refresh(task)
        assert expired == 1
        assert task.status == "expired"
        assert task.completed_at is not None

    def test_finished_work_is_left_alone(self, db):
        task = browser_tasks.enqueue(db, "ping")
        browser_tasks.lease(db, agent_id="a1")
        browser_tasks.complete(db, task.id, {"ok": True})
        task.expires_at = _now() - timedelta(days=1)
        db.commit()

        browser_tasks.reap(db)
        db.refresh(task)
        assert task.status == "done", "a completed task must not be expired after the fact"


class TestComplete:
    def test_records_the_result(self, db):
        task = browser_tasks.enqueue(db, "ping")
        browser_tasks.lease(db, agent_id="a1")
        done = browser_tasks.complete(db, task.id, {"pong": True}, agent_id="a1")
        assert done.status == "done"
        assert done.result == {"pong": True}
        assert done.completed_at is not None
        assert done.lease_expires_at is None

    def test_cannot_complete_unleased_work(self, db):
        task = browser_tasks.enqueue(db, "ping")
        with pytest.raises(TaskError) as exc:
            browser_tasks.complete(db, task.id, {})
        assert "not leased" in str(exc.value)

    def test_cannot_complete_twice(self, db):
        task = browser_tasks.enqueue(db, "ping")
        browser_tasks.lease(db, agent_id="a1")
        browser_tasks.complete(db, task.id, {})
        with pytest.raises(TaskError) as exc:
            browser_tasks.complete(db, task.id, {})
        assert "already done" in str(exc.value)

    def test_a_stale_agent_cannot_overwrite_the_current_one(self, db):
        # The lease lapsed, someone else picked it up, and now the original
        # agent finally reports back. Its result is for work that was redone.
        task = browser_tasks.enqueue(db, "ping")
        browser_tasks.lease(db, agent_id="slow")
        task.lease_expires_at = _now() - timedelta(seconds=1)
        db.commit()
        browser_tasks.lease(db, agent_id="fast")

        with pytest.raises(TaskError) as exc:
            browser_tasks.complete(db, task.id, {"stale": True}, agent_id="slow")
        assert "different agent" in str(exc.value)

    def test_unknown_task_is_refused(self, db):
        with pytest.raises(TaskError):
            browser_tasks.complete(db, "11111111-1111-1111-1111-111111111111", {})

    def test_malformed_id_is_refused(self, db):
        with pytest.raises(TaskError):
            browser_tasks.complete(db, "not-a-uuid", {})


class TestFail:
    def test_requeues_while_attempts_remain(self, db):
        task = browser_tasks.enqueue(db, "ping", max_attempts=3)
        browser_tasks.lease(db, agent_id="a1")
        failed = browser_tasks.fail(db, task.id, "page not ready", agent_id="a1")
        assert failed.status == "queued"
        assert failed.error == "page not ready"
        assert failed.agent_id is None

    def test_retires_after_max_attempts(self, db):
        task = browser_tasks.enqueue(db, "ping", max_attempts=2)
        for _ in range(2):
            browser_tasks.lease(db, agent_id="a1")
            result = browser_tasks.fail(db, task.id, "still broken", agent_id="a1")
        assert result.status == "failed"
        assert result.completed_at is not None

    def test_retired_work_is_not_leased_again(self, db):
        task = browser_tasks.enqueue(db, "ping", max_attempts=1)
        browser_tasks.lease(db, agent_id="a1")
        browser_tasks.fail(db, task.id, "no", agent_id="a1")
        assert browser_tasks.lease(db, agent_id="a1") == []

    def test_a_refusal_is_not_retried(self, db):
        # A 403 will be a 403 again. Three identical rows bury whatever else
        # failed that hour — which is exactly what four Reddit searches turning
        # into twelve failures did.
        task = browser_tasks.enqueue(db, "ping", max_attempts=3)
        browser_tasks.lease(db, agent_id="a1")
        failed = browser_tasks.fail(
            db, task.id, "HTTP 403 from reddit.com", agent_id="a1", permanent=True
        )
        assert failed.status == "failed"
        assert failed.attempts == 1, "retired on the first attempt, not the third"

    def test_a_permanently_failed_task_is_not_leased_again(self, db):
        task = browser_tasks.enqueue(db, "ping", max_attempts=3)
        browser_tasks.lease(db, agent_id="a1")
        browser_tasks.fail(db, task.id, "HTTP 403", agent_id="a1", permanent=True)
        assert browser_tasks.lease(db, agent_id="a1") == []

    def test_an_ordinary_failure_still_retries(self, db):
        task = browser_tasks.enqueue(db, "ping", max_attempts=3)
        browser_tasks.lease(db, agent_id="a1")
        failed = browser_tasks.fail(db, task.id, "timed out", agent_id="a1")
        assert failed.status == "queued"

    def test_a_failure_with_no_message_still_says_something(self, db):
        task = browser_tasks.enqueue(db, "ping")
        browser_tasks.lease(db, agent_id="a1")
        failed = browser_tasks.fail(db, task.id, "", agent_id="a1")
        assert failed.error


class TestHeartbeat:
    def test_extends_the_lease(self, db):
        task = browser_tasks.enqueue(db, "ping")
        leased = browser_tasks.lease(db, agent_id="a1")[0]
        original = leased.lease_expires_at
        leased.lease_expires_at = _now() + timedelta(seconds=1)
        db.commit()

        beat = browser_tasks.heartbeat(db, task.id, agent_id="a1")
        assert beat.lease_expires_at > original - timedelta(seconds=1)
        assert beat.status == "leased"


class TestQueueStats:
    def test_counts_every_status(self, db):
        browser_tasks.enqueue(db, "ping")
        done = browser_tasks.enqueue(db, "ping")
        browser_tasks.lease(db, agent_id="a1", limit=2)
        browser_tasks.complete(db, done.id, {}, agent_id="a1")

        stats = browser_tasks.queue_stats(db)
        assert stats["done"] == 1
        assert stats["leased"] == 1
        # Every status is present even at zero, so a caller can render the row
        # without checking for missing keys.
        assert set(stats) >= {"queued", "leased", "done", "failed", "expired"}

    def test_empty_queue_reports_zeroes(self, db):
        assert browser_tasks.queue_stats(db)["queued"] == 0


class TestSerialization:
    def test_as_dict_gives_the_agent_what_it_needs(self, db):
        task = browser_tasks.enqueue(db, "ping", {"url": "x"})
        payload = task.as_dict()
        assert payload["kind"] == "ping"
        assert payload["payload"] == {"url": "x"}
        assert payload["id"] == str(task.id)

    def test_as_dict_withholds_internals(self, db):
        task = browser_tasks.enqueue(db, "ping")
        # The agent has no use for these and no business seeing which other
        # engine last held the task.
        assert not {"agent_id", "error", "result"} & set(task.as_dict())
