"""
What the browser extension did, written down.

Every question about this subsystem has started the same way for months: is it
even installed? A harvest that found nothing, an autofill that recognised two
fields out of fifteen, an overlay lookup on a site the URL matcher cannot
resolve — all of them happen on someone else's page and leave no trace here,
and that silence is indistinguishable from an extension that was uninstalled a
week ago.

Two rules, both borrowed from `llm_log` for the same reasons:

* **It never raises.** Recording that a harvest happened must not be able to
  break the harvest. Every entry point here swallows its own failures and logs.
* **It has a ceiling.** Rows are pruned on a timer. A diagnostic that fills the
  disk stops being a diagnostic.

One rule that is its own: **it stores hosts, not URLs.** The URL is the
posting, which is already in `jobs`; the host is what you group by when asking
which sites the extension is failing on. It also keeps this from being a
browsing history, which is not a thing anyone needs kept.
"""

import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from sqlalchemy import Integer

from app.config import settings
from app.models.agent_event import KINDS

logger = logging.getLogger(__name__)

# What a client may send in one batch. The extension buffers events while the
# server is unreachable, so a batch after an outage is legitimately large — but
# not unbounded.
MAX_BATCH = 50

# Kept small: a summary is a handful of counts, not a payload.
_MAX_SUMMARY_KEYS = 20
_MAX_VALUE_CHARS = 300


def host_of(url: str | None) -> str | None:
    """The site an event happened on, or None if the URL says nothing."""
    if not url:
        return None
    try:
        host = (urlparse(str(url)).hostname or "").lower()
    except (ValueError, AttributeError):
        return None
    if not host:
        return None
    return host[:160]


def _clean_kind(kind) -> str:
    text = str(kind or "").strip().lower()
    # An extension newer than the server still leaves a trace, filed under
    # `other`. Rejecting it would mean an upgrade silently loses its events.
    return text if text in KINDS else "other"


def _clean_summary(summary) -> dict | None:
    """A summary small enough to keep a million of."""
    if not isinstance(summary, dict) or not summary:
        return None
    out = {}
    for key, value in list(summary.items())[:_MAX_SUMMARY_KEYS]:
        name = str(key)[:60]
        if isinstance(value, bool) or isinstance(value, (int, float)) or value is None:
            out[name] = value
        elif isinstance(value, (list, tuple)):
            out[name] = [str(item)[:80] for item in list(value)[:20]]
        else:
            out[name] = str(value)[:_MAX_VALUE_CHARS]
    return out


def record(db, kind: str, *, host: str | None = None, url: str | None = None,
           agent_id: str = "", ok: bool = True, summary: dict | None = None):
    """
    Store one event. Never raises; returns the row or None.

    Written on the caller's session rather than its own, unlike `llm_log`.
    That log deliberately survives the rollback of what it is diagnosing; this
    records that something happened, and an event describing a harvest that was
    rolled back is a false record rather than a useful one.
    """
    try:
        from app.models.agent_event import AgentEvent

        row = AgentEvent(
            kind=_clean_kind(kind),
            host=host_of(url) if host is None else (str(host)[:160] or None),
            agent_id=(str(agent_id)[:120] or None) if agent_id else None,
            ok=bool(ok),
            summary=_clean_summary(summary),
        )
        db.add(row)
        return row
    except Exception as exc:
        logger.warning("agent_events: could not record a %s event: %s", kind, exc)
        return None


def record_batch(db, events, agent_id: str = "") -> dict:
    """
    Store a batch reported by a client. Returns what it made of it.

    The extension buffers events while the server is unreachable, so this is
    the path most of them arrive by, and it must be forgiving: a malformed
    entry costs that entry, not the batch.
    """
    if not isinstance(events, list):
        return {"stored": 0, "rejected": 0}

    stored = 0
    rejected = 0
    for entry in events[:MAX_BATCH]:
        if not isinstance(entry, dict):
            rejected += 1
            continue
        row = record(
            db,
            entry.get("kind"),
            host=entry.get("host"),
            url=entry.get("url"),
            agent_id=str(entry.get("agent_id") or agent_id),
            ok=entry.get("ok", True),
            summary=entry.get("summary"),
        )
        if row is None:
            rejected += 1
        else:
            stored += 1

    if stored:
        try:
            db.commit()
        except Exception as exc:
            db.rollback()
            logger.warning("agent_events: batch commit failed: %s", exc)
            return {"stored": 0, "rejected": rejected + stored}
    return {"stored": stored, "rejected": rejected + max(0, len(events) - MAX_BATCH)}


# ---------------------------------------------------------------------------
# Reading it back
# ---------------------------------------------------------------------------

def _window_start(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=max(1, days))


def summary(db, days: int = 7) -> dict:
    """
    What the extension has been doing, shaped for the panel that shows it.

    Every number here answers a question that previously had no answer at all,
    so the shape is "one section per question" rather than a generic dump.
    """
    from sqlalchemy import case, func

    from app.models.agent_event import AgentEvent

    since = _window_start(days)

    rows = (
        db.query(
            AgentEvent.kind,
            func.count(AgentEvent.id),
            func.sum(case((AgentEvent.ok.is_(False), 1), else_=0)),
        )
        .filter(AgentEvent.created_at >= since)
        .group_by(AgentEvent.kind)
        .all()
    )
    by_kind = [
        {"kind": kind, "count": int(count or 0), "failed": int(failed or 0)}
        for kind, count, failed in sorted(rows, key=lambda r: -(r[1] or 0))
    ]

    # Which sites the extension is failing on. The single most useful view
    # here: a host that fails every time is a host that needs a different
    # approach, and without this it just looks like the extension is flaky.
    failing = (
        db.query(AgentEvent.host, func.count(AgentEvent.id))
        .filter(
            AgentEvent.created_at >= since,
            AgentEvent.ok.is_(False),
            AgentEvent.host.isnot(None),
        )
        .group_by(AgentEvent.host)
        .order_by(func.count(AgentEvent.id).desc())
        .limit(8)
        .all()
    )

    return {
        "days": days,
        "total": sum(entry["count"] for entry in by_kind),
        "by_kind": by_kind,
        "failing_hosts": [{"host": host, "count": int(count)} for host, count in failing],
        "harvest": harvest_yield(db, days=days),
        "recent": recent(db, limit=12),
    }


def harvest_yield(db, days: int = 7) -> dict:
    """
    What browsing actually contributed.

    The extension's whole claim is that jobs a server cannot reach arrive
    anyway because a person looked at them. This is that claim as a number.
    """
    from sqlalchemy import func

    from app.models.agent_event import AgentEvent

    rows = (
        db.query(
            func.count(AgentEvent.id),
            func.sum(func.coalesce(AgentEvent.summary["found"].astext.cast(Integer), 0)),
            func.sum(func.coalesce(AgentEvent.summary["inserted"].astext.cast(Integer), 0)),
        )
        .filter(
            AgentEvent.created_at >= _window_start(days),
            AgentEvent.kind == "harvest",
        )
        .first()
    )
    posts, found, inserted = rows or (0, 0, 0)
    return {
        "posts": int(posts or 0),
        "found": int(found or 0),
        "inserted": int(inserted or 0),
    }


def recent(db, limit: int = 12) -> list:
    from app.models.agent_event import AgentEvent

    return (
        db.query(AgentEvent)
        .order_by(AgentEvent.created_at.desc())
        .limit(max(1, limit))
        .all()
    )


def agents(db, days: int = 7) -> list[dict]:
    """
    Every browser that has reported in, not just the last one.

    The profile blob this replaces remembered exactly one agent, so a laptop
    and a desktop overwrote each other and "the extension has not polled since
    Tuesday" was reported about whichever happened to be second.
    """
    from sqlalchemy import func

    from app.models.agent_event import AgentEvent

    rows = (
        db.query(
            AgentEvent.agent_id,
            func.max(AgentEvent.created_at),
            func.count(AgentEvent.id),
        )
        .filter(
            AgentEvent.created_at >= _window_start(days),
            AgentEvent.agent_id.isnot(None),
        )
        .group_by(AgentEvent.agent_id)
        .order_by(func.max(AgentEvent.created_at).desc())
        .all()
    )
    return [
        {"agent_id": agent_id, "last_seen": last_seen, "events": int(count or 0)}
        for agent_id, last_seen, count in rows
    ]


def prune(db, keep: int | None = None) -> int:
    """Drop the oldest rows beyond the retention limit. Returns how many went."""
    from app.models.agent_event import AgentEvent

    keep = keep if keep is not None else int(
        getattr(settings, "AGENT_EVENT_KEEP_ROWS", 20000)
    )
    if keep <= 0:
        return 0
    total = db.query(AgentEvent).count()
    if total <= keep:
        return 0
    cutoff = (
        db.query(AgentEvent.created_at)
        .order_by(AgentEvent.created_at.desc())
        .offset(keep - 1)
        .limit(1)
        .scalar()
    )
    if cutoff is None:
        return 0
    removed = (
        db.query(AgentEvent)
        .filter(AgentEvent.created_at < cutoff)
        .delete(synchronize_session=False)
    )
    db.commit()
    if removed:
        logger.info("agent_events: pruned %d old events (keeping %d)", removed, keep)
    return removed
