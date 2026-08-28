"""
The queue behind /api/agent/*.

Kept out of the router so the interesting parts — who gets a task when two
agents ask at once, what happens to work an agent abandons — can be tested
without HTTP in the way.

The one rule worth stating plainly: **a task may be handed to exactly one agent
at a time**. Two engines run on the same laptop and both poll, so "probably
fine" leasing means the same job link gets resolved twice and, worse, that two
agents both post a result for one task. `SELECT ... FOR UPDATE SKIP LOCKED` is
what makes the claim atomic — concurrent leasers step over rows already spoken
for rather than blocking on them or duplicating them.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.config import settings
from app.models.browser_task import TASK_KINDS, TASK_STATUSES, BrowserTask

logger = logging.getLogger(__name__)


class TaskError(Exception):
    """A queue operation that could not proceed, phrased for the agent."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _lease_seconds() -> int:
    return max(10, int(getattr(settings, "AGENT_LEASE_SECONDS", 120)))


def _ttl_hours() -> int:
    return max(1, int(getattr(settings, "AGENT_TASK_TTL_HOURS", 24)))


# ---------------------------------------------------------------------------
# Producing work
# ---------------------------------------------------------------------------

def enqueue(
    db,
    kind: str,
    payload: dict | None = None,
    *,
    priority: int = 0,
    max_attempts: int = 3,
    ttl_hours: int | None = None,
) -> BrowserTask:
    """
    Add one task to the queue.

    Callers are pipeline code that must not care whether an agent exists. This
    never blocks, never checks for a listener, and never raises because nobody
    is home.
    """
    if kind not in TASK_KINDS:
        raise TaskError(f"Unknown task kind {kind!r}. Known kinds: {', '.join(TASK_KINDS)}")

    task = BrowserTask(
        kind=kind,
        payload=payload or {},
        status="queued",
        priority=priority,
        max_attempts=max(1, max_attempts),
        expires_at=_now() + timedelta(hours=ttl_hours or _ttl_hours()),
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


# ---------------------------------------------------------------------------
# Consuming work
# ---------------------------------------------------------------------------

def reap(db) -> tuple[int, int]:
    """
    Return abandoned work to the queue and retire work that went stale.

    Runs at the top of every lease. Putting it here rather than on a schedule
    means the queue self-heals whenever anyone asks for work, and a deployment
    that never runs an agent never accumulates anything to heal.

    Returns (leases_recovered, tasks_expired).
    """
    now = _now()

    # A lapsed lease is not a failure — the laptop closed. Return it to the
    # queue without counting an attempt against it, or a flaky connection would
    # burn through max_attempts without the task ever having run.
    recovered = (
        db.query(BrowserTask)
        .filter(
            BrowserTask.status == "leased",
            BrowserTask.lease_expires_at.isnot(None),
            BrowserTask.lease_expires_at < now,
        )
        .update(
            {"status": "queued", "agent_id": None, "lease_expires_at": None},
            synchronize_session=False,
        )
    )

    expired = (
        db.query(BrowserTask)
        .filter(
            BrowserTask.status.in_(("queued", "leased")),
            BrowserTask.expires_at < now,
        )
        .update(
            {"status": "expired", "completed_at": now, "lease_expires_at": None},
            synchronize_session=False,
        )
    )

    if recovered or expired:
        db.commit()
        logger.info("browser_tasks.reap: recovered %d, expired %d", recovered, expired)
    return recovered, expired


def lease(
    db,
    kinds: list[str] | None = None,
    *,
    agent_id: str = "",
    limit: int = 1,
) -> list[BrowserTask]:
    """
    Claim up to `limit` queued tasks for `agent_id`.

    Returns immediately with whatever is available, including nothing. The
    waiting is the router's job — this is the part that must be atomic, and
    holding a row lock open across a 25-second poll would be a good way to
    deadlock the queue.
    """
    reap(db)

    limit = max(1, min(int(limit), int(getattr(settings, "AGENT_MAX_LEASE_BATCH", 10))))
    now = _now()

    stmt = (
        select(BrowserTask)
        .where(BrowserTask.status == "queued", BrowserTask.expires_at > now)
        .order_by(BrowserTask.priority.desc(), BrowserTask.created_at.asc())
        .limit(limit)
        # The claim is atomic because of these two: FOR UPDATE takes the rows,
        # SKIP LOCKED means a second agent asking at the same moment walks past
        # them to the next available work instead of waiting or duplicating.
        .with_for_update(skip_locked=True)
    )
    if kinds:
        unknown = [k for k in kinds if k not in TASK_KINDS]
        if unknown:
            raise TaskError(f"Unknown task kind(s): {', '.join(sorted(unknown))}")
        stmt = stmt.where(BrowserTask.kind.in_(kinds))

    tasks = list(db.execute(stmt).scalars().all())
    for task in tasks:
        task.status = "leased"
        task.agent_id = agent_id or None
        task.leased_at = now
        task.lease_expires_at = now + timedelta(seconds=_lease_seconds())
        task.attempts += 1
    db.commit()
    for task in tasks:
        db.refresh(task)
    return tasks


def _held(db, task_id, agent_id: str = "") -> BrowserTask:
    """The leased task an agent is reporting on, or an explanation."""
    try:
        key = uuid.UUID(str(task_id))
    except (ValueError, AttributeError, TypeError):
        raise TaskError("That is not a task id.") from None

    task = db.query(BrowserTask).filter(BrowserTask.id == key).first()
    if not task:
        raise TaskError("No such task.")
    if task.is_terminal:
        raise TaskError(f"That task is already {task.status}.")
    if task.status != "leased":
        raise TaskError("That task is not leased, so there is no result to report.")
    # Reject a result from an agent that no longer holds the lease. Without this
    # a slow agent could overwrite the work of the one that took over from it.
    if agent_id and task.agent_id and task.agent_id != agent_id:
        raise TaskError("That task is leased to a different agent.")
    return task


def complete(db, task_id, result: dict | None = None, *, agent_id: str = "") -> BrowserTask:
    task = _held(db, task_id, agent_id)
    task.status = "done"
    task.result = result or {}
    task.error = None
    task.completed_at = _now()
    task.lease_expires_at = None
    db.commit()
    db.refresh(task)

    # Acting on the result happens after it is durably recorded, so an ingestion
    # bug cannot lose work the agent already did. `ingest` swallows its own
    # failures for the same reason.
    from app.services.agent_work import ingest

    ingest(db, task)
    _note_event(db, task, "task_done", ok=True)
    return task


def _note_event(db, task: BrowserTask, kind: str, *, ok: bool) -> None:
    """
    File a finished task in the event log as well as on the task row.

    The row is the detail and is pruned in a fortnight; the event is the count
    and outlives it. "Which task kinds actually succeed on which hosts" is a
    question about months, and it was previously answerable only for as long as
    nobody deleted anything — which, since nothing ever pruned this table, was
    both the reason it worked and the reason the table grew without bound.
    """
    from app.services import agent_events

    payload = task.payload or {}
    agent_events.record(
        db, kind,
        url=payload.get("url") or "",
        agent_id=task.agent_id or "",
        ok=ok,
        summary={
            "task_kind": task.kind,
            "attempts": task.attempts,
            **({"error": (task.error or "")[:300]} if not ok else {}),
            **({"ingest": (task.result or {}).get("ingest")}
               if isinstance((task.result or {}).get("ingest"), dict) else {}),
        },
    )
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.warning("browser_tasks: could not record a %s event: %s", kind, exc)


def fail(
    db, task_id, error: str, *, agent_id: str = "", permanent: bool = False
) -> BrowserTask:
    """
    Record a failed attempt.

    Requeues while attempts remain, because the common failures here are
    transient — a page that had not finished loading, a session that needed
    re-auth. Past that, the task is retired with the last error kept, since a
    task retried forever is indistinguishable from a queue that is broken.

    `permanent` skips the retries entirely. A site answering 403 will answer 403
    again, so trying twice more turns one refusal into three rows of the same
    message and buries whatever else went wrong that hour.
    """
    task = _held(db, task_id, agent_id)
    task.error = (error or "")[:2000] or "The agent reported a failure with no message."
    task.agent_id = None
    task.lease_expires_at = None

    retired = permanent or task.attempts >= task.max_attempts
    if retired:
        task.status = "failed"
        task.completed_at = _now()
    else:
        task.status = "queued"
    db.commit()
    db.refresh(task)
    # Only when it is actually giving up. A requeued attempt is the retry
    # working, and counting each one as a failure would make every transient
    # hiccup look like a broken host.
    if retired:
        _note_event(db, task, "task_failed", ok=False)
    return task


def heartbeat(db, task_id, *, agent_id: str = "") -> BrowserTask:
    """Extend a lease that is still being worked on."""
    task = _held(db, task_id, agent_id)
    task.lease_expires_at = _now() + timedelta(seconds=_lease_seconds())
    db.commit()
    db.refresh(task)
    return task


# ---------------------------------------------------------------------------
# Visibility
# ---------------------------------------------------------------------------

def recent(db, limit: int = 10) -> list[BrowserTask]:
    """The last few tasks, newest first, for the status panel."""
    return (
        db.query(BrowserTask)
        .order_by(BrowserTask.created_at.desc())
        .limit(max(1, min(limit, 50)))
        .all()
    )


def record_agent_seen(db, agent_id: str, kinds: list[str] | None,
                      harvest_sites: list[str] | None = None) -> None:
    """
    Note that an agent asked for work, and what it said it could run.

    Recorded on every poll rather than on every lease, because an agent polling
    an empty queue used to leave no trace at all — so "no agent is running" and
    "no work has come up" were the same silence, and the wrong one was usually
    assumed.

    The kinds matter more than the timestamp. An agent that quietly does not
    offer a kind never claims that work, and the tasks sit queued forever
    looking like a server-side problem. Writing down what it offered turns that
    into something visible on a page.

    Never waits for the row
    -----------------------
    This writes the profile blob, which is the hottest row in the schema — the
    fetch cycle, the mailbox poller and a settings save all update it. Without
    a timeout, a poll that arrives while something else holds that row waits
    indefinitely, and Postgres queues the next poll behind it: one lease
    request per minute, none of them completing, each abandoned by the client
    after forty seconds. Twenty-two of them stacked up in the wild before
    anybody noticed, and the whole browser agent was dead the entire time —
    because the *diagnostic* was blocking the work it was meant to describe.

    So the wait is bounded and losing is fine. A missing timestamp costs a line
    on a status panel. A blocked lease costs every browser task there is.
    """
    from sqlalchemy import text

    from app.models.profile import Profile

    try:
        # Half a second: long enough to win an uncontended row, far too short to
        # queue behind a fetch cycle. `SET LOCAL` expires with the transaction,
        # so this cannot leak onto a pooled connection's next borrower.
        db.execute(text("SET LOCAL lock_timeout = '500ms'"))
    except Exception as exc:
        logger.debug("browser_tasks: could not set a lock timeout: %s", exc)

    profile = db.query(Profile).first()
    if profile is None:
        return
    name = agent_id or "anonymous"
    seen = {
        "agent_id": name,
        "kinds": sorted(kinds or []),
        # Hosts the harvest reader is actually registered on. The server cannot
        # work this out for itself, and without it a board that opened pages
        # and forwarded nothing got the same two-part shrug either way — the
        # box is unticked, *or* its pages fetch from somewhere we do not watch.
        # Those want completely different fixes and only the browser knows
        # which one applies.
        "harvest_sites": sorted(harvest_sites or []),
        "at": _now().isoformat(),
    }
    data = dict(profile.data or {})
    # A map, not a slot. There is routinely more than one browser — a laptop
    # and a desktop — and a single slot meant they overwrote each other, so
    # "nothing has polled since Tuesday" got reported about whichever happened
    # to be second. `agent` stays alongside it as the most recent, because the
    # status panel and `last_agent` read it.
    agents = dict(data.get("agents") or {})
    agents[name[:120]] = seen
    if len(agents) > 12:
        # A cap, because an agent_id that is regenerated per browser session
        # would otherwise grow this blob forever.
        agents = dict(
            sorted(agents.items(), key=lambda item: item[1].get("at") or "",
                   reverse=True)[:12]
        )
    data["agents"] = agents
    data["agent"] = seen
    profile.data = data

    try:
        db.commit()
    except Exception as exc:
        # Rolled back here rather than left to the caller. The lease that
        # called this goes on to query the queue on the same session, and a
        # session left in a failed transaction turns "we could not write a
        # timestamp" into "this agent gets no work".
        db.rollback()
        logger.info(
            "browser_tasks: skipped recording presence for %s — the profile row "
            "was busy (%s)", name, str(exc).splitlines()[0][:120],
        )


def last_agent(db) -> dict | None:
    """
    Which agent last asked for work, when, and what it offered to run.

    Falls back to the last lease for a deployment whose agent has not polled
    since this was added — an older record is better than implying nothing has
    ever connected.
    """
    from app.models.profile import Profile

    profile = db.query(Profile).first()
    seen = ((profile.data if profile else {}) or {}).get("agent")
    if seen and seen.get("at"):
        return {
            "agent_id": seen.get("agent_id") or "anonymous",
            "at": seen["at"],
            "kinds": seen.get("kinds") or [],
            "polled": True,
        }

    task = (
        db.query(BrowserTask)
        .filter(BrowserTask.leased_at.isnot(None))
        .order_by(BrowserTask.leased_at.desc())
        .first()
    )
    if not task:
        return None
    return {
        "agent_id": task.agent_id or "anonymous",
        "at": task.leased_at.isoformat(),
        "kinds": [],
        "polled": False,
    }


def prune(db, days: int | None = None) -> int:
    """
    Drop finished tasks older than the retention window.

    This table has never been pruned, and it takes a row for every link
    resolution and every in-browser enrichment — which is most of what the
    browser tier does. It grew without bound because a finished task was the
    only record that the work happened at all; now that `agent_events` keeps
    the countable history, the row itself is just the payload and the page it
    came back with, and those are the large parts.

    Returns how many were removed.
    """
    days = days if days is not None else int(
        getattr(settings, "BROWSER_TASK_KEEP_DAYS", 14)
    )
    if days <= 0:
        return 0
    cutoff = _now() - timedelta(days=days)
    removed = (
        db.query(BrowserTask)
        .filter(
            # Terminal only. A queued task is not old, it is late, and deleting
            # it would silently drop work nobody has done yet.
            BrowserTask.status.in_(("done", "failed", "expired")),
            BrowserTask.completed_at.isnot(None),
            BrowserTask.completed_at < cutoff,
        )
        .delete(synchronize_session=False)
    )
    db.commit()
    if removed:
        logger.info("browser_tasks: pruned %d finished task(s) older than %d days",
                    removed, days)
    return removed


def reading_hosts(db) -> set[str]:
    """
    Every host some browser's harvest reader is registered on.

    The union across browsers on purpose: a laptop reading Dice and a desktop
    that is not means Dice *is* being read, and reporting it as unread because
    one of them has the box unticked would be wrong.
    """
    from app.models.profile import Profile

    profile = db.query(Profile).first()
    if profile is None:
        return set()

    hosts: set[str] = set()
    for seen in (dict((profile.data or {}).get("agents") or {})).values():
        for host in (seen or {}).get("harvest_sites") or []:
            cleaned = str(host or "").strip().lower()
            if cleaned:
                hosts.add(cleaned)
    return hosts


def known_agents(db) -> list[dict]:
    """
    Every browser that has polled, newest first.

    `last_agent` answers "is anything connected"; this answers "which of them",
    which is the question that matters once there is more than one — an agent
    that stopped a week ago is invisible behind one that polled a second ago.
    """
    from app.models.profile import Profile

    profile = db.query(Profile).first()
    stored = ((profile.data if profile else {}) or {}).get("agents") or {}
    if not isinstance(stored, dict):
        return []
    rows = [entry for entry in stored.values() if isinstance(entry, dict)]
    return sorted(rows, key=lambda entry: entry.get("at") or "", reverse=True)


def queue_stats(db) -> dict:
    """Counts by status, for the options page and for debugging."""
    rows = (
        db.query(BrowserTask.status, func.count(BrowserTask.id))
        .group_by(BrowserTask.status)
        .all()
    )
    stats = dict.fromkeys(TASK_STATUSES, 0)
    for status, count in rows:
        stats[status] = count
    return stats
