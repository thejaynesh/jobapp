"""
A lease must never be held up by the note saying an agent asked for one.

`/api/agent/lease` records agent presence before it looks at the queue, and
that write lands on the profile blob — the hottest row in the schema, also
written by the fetch cycle, the mailbox poller and every settings save. With no
timeout, a poll arriving while something else held that row waited
indefinitely, and Postgres queued the next poll behind it.

Found in the wild: twenty-two `UPDATE profiles` backends stacked on one lock,
one arriving per minute (the extension's alarm), the oldest waiting
twenty-two minutes, each abandoned by the client after forty seconds. The
browser agent was completely dead for the whole period — no leases, so no
browse tasks, no crawls, no harvest queue — and it presented as "the extension
isn't doing anything", because the diagnostic was blocking the work it existed
to describe.

The rule these tests hold down: **presence is best-effort, the lease is not.**
Failing to write a timestamp costs a line on a status panel. Failing to lease
costs every browser task there is.
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.models.browser_task import BrowserTask
from app.models.profile import Profile
from app.services import browser_tasks


@pytest.fixture
def profile(db):
    row = Profile(data={"target_roles": ["Backend Engineer"]})
    db.add(row)
    db.commit()
    return row


class TestPresenceIsBestEffort:
    def test_it_records_when_the_row_is_free(self, db, profile):
        browser_tasks.record_agent_seen(db, "laptop", ["ping", "browse_page"])
        db.refresh(profile)

        assert profile.data["agent"]["agent_id"] == "laptop"
        assert profile.data["agents"]["laptop"]["kinds"] == ["browse_page", "ping"]

    def test_a_busy_row_costs_the_timestamp_and_nothing_else(self, db, profile):
        # Standing in for a lock timeout: the write fails, and the only correct
        # consequence is that the note is missing.
        with patch.object(db, "commit", side_effect=RuntimeError("lock timeout")):
            browser_tasks.record_agent_seen(db, "laptop", ["ping"])

        # No exception escaped — the caller goes on to look at the queue.

    def test_a_failed_write_leaves_the_session_usable(self, db, profile):
        # The load-bearing one. The lease queries the queue on this same
        # session, so a session left in a failed transaction turns "could not
        # write a timestamp" into "this agent gets no work" — which is the
        # outage, arrived at from the other direction.
        calls = {"n": 0}
        real_commit = db.commit

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("lock timeout")
            return real_commit()

        with patch.object(db, "commit", side_effect=flaky):
            browser_tasks.record_agent_seen(db, "laptop", ["ping"])

        # The session still works, which is what the lease needs.
        assert db.query(Profile).first() is not None

    def test_the_wait_is_bounded(self, db, profile):
        # Asserted on the statement rather than by contending for real: a test
        # that took a competing lock would need a second connection and would
        # hang for as long as the bug it is checking for.
        statements = []
        real_execute = db.execute

        def watched(statement, *args, **kwargs):
            statements.append(str(statement))
            return real_execute(statement, *args, **kwargs)

        with patch.object(db, "execute", side_effect=watched):
            browser_tasks.record_agent_seen(db, "laptop", ["ping"])

        assert any("lock_timeout" in text for text in statements)

    def test_a_missing_profile_is_not_an_error(self, db):
        browser_tasks.record_agent_seen(db, "laptop", ["ping"])


class TestTheLeaseStillWorks:
    def test_work_is_handed_out_after_a_failed_presence_write(self, db, profile):
        # The whole point: the queue is what the request is for.
        browser_tasks.enqueue(db, "ping", {"from": "test"})

        with patch.object(browser_tasks, "record_agent_seen",
                          side_effect=RuntimeError("lock timeout")):
            try:
                browser_tasks.record_agent_seen(db, "laptop", ["ping"])
            except RuntimeError:
                pass

        leased = browser_tasks.lease(db, ["ping"], agent_id="laptop", limit=1)
        assert len(leased) == 1

    def test_presence_is_written_once_per_request_not_per_attempt(self):
        # The poll re-checks the queue every second for twenty-five seconds. If
        # presence were written per attempt, one lease would be twenty-five
        # writes to the hottest row in the schema.
        source = open("app/routers/agent.py").read()
        loop = source.split("deadline = time.monotonic() + wait")[1]
        assert "record_agent_seen" not in loop


class TestTheFetchCycleDoesNotSitOnTheRow:
    def test_the_profile_write_comes_after_the_job_loop(self):
        # It used to be assigned before the loop, and `begin_nested()` — one
        # savepoint per job — flushes pending changes first. So the UPDATE
        # landed immediately and held an exclusive lock until the commit after
        # every insert, which on a full cycle is minutes.
        #
        # Checked on the source because the property is *when* the write
        # happens relative to the loop, and a behavioural test would have to
        # observe lock duration from a second connection.
        source = open("app/services/job_fetcher.py").read()
        body = source.split("def _update_board_registry")[1]

        assign = body.rfind("profile.data = ")
        loop = body.rfind("counts[\"inserted\"] += 1")
        assert assign > loop, (
            "the profile write moved back above the job loop, which reinstates "
            "the lock that stalled every agent poll"
        )

    def test_it_re_reads_before_merging(self):
        # The cycle runs for minutes and the agent poll writes the same blob
        # every one of them, so the copy taken at the start is stale by the
        # end. Writing it wholesale would revert every concurrent writer.
        source = open("app/services/job_fetcher.py").read()
        tail = source.split("counts[\"boards\"] = board_stats")[-1]
        assert "db.refresh(profile)" in tail
