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
        # The payload comparison is not bounded by this panel's window — it
        # judges each site against its own last N payloads rather than against
        # a stretch of calendar, and narrowing that to a week would throw the
        # comparison away. The page count is bounded, because it is a plain
        # count shown next to other plain counts over this window.
        "harvest_health": harvest_health(db, pages_days=days),
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


# What "lately" means for one site: its most recent N forwarded payloads.
#
# Counted rather than dated, and that is the whole design decision here. A
# calendar split — this week against last week — cannot say anything at all
# until a week of history exists, and then says "not browsed lately" about a
# site you used heavily on Monday and not since. Counting payloads makes the
# comparison relative to *your browsing* instead of to the clock: thirty job
# pages on one afternoon is a complete before-and-after, and a site you open
# twice a month is judged on its own last thirty rather than declared broken
# for being quiet.
_RECENT_PAYLOADS = 25

# Below this many recent payloads, "it found nothing" is not worth saying: a
# handful of page loads is as likely to be a homepage as a broken reader.
_MIN_PAYLOADS_TO_JUDGE = 15

# An outer bound so the "before" is not something from six months ago. Wide,
# because the payload count is what actually splits the two halves.
_MAX_WINDOW_DAYS = 120

HEALTH_LABELS = {
    "healthy": "Working",
    "regressed": "Stopped finding jobs",
    "silent": "Forwarding, never finds jobs",
    "unread": "Pages opened, nothing forwarded",
    "quiet": "Not browsed enough to say",
}


def read_stats(db, days: int) -> dict[str, dict]:
    """
    Per host: how many responses the page's reader actually looked at.

    The denominator every one of these diagnoses was missing. A harvest event
    only exists when something got through, so a site that opened, scrolled,
    paginated and forwarded nothing left no harvest row — and the panel could
    only report the absence, which reads the same whether the extension was
    switched off, the page answered in no JSON at all, or forty job payloads
    arrived and the URL filter threw all of them away.

    Those want three different fixes, and the counts here are what tell them
    apart without a DevTools capture.
    """
    from sqlalchemy import case, func

    from app.models.agent_event import AgentEvent

    def total(key: str):
        return func.sum(
            func.coalesce(AgentEvent.summary[key].astext.cast(Integer), 0)
        )

    # Pages, not reports. Each page reports twice — once early, once as it
    # unloads with whatever arrived after — so counting rows would say a site
    # was visited twice as often as it was.
    pages = func.sum(
        case((AgentEvent.summary["first"].astext == "true", 1), else_=0)
    )

    rows = (
        db.query(
            AgentEvent.host,
            pages,
            func.count(AgentEvent.id),
            total("json"),
            total("sent"),
            total("probed"),
            total("url_no"),
        )
        .filter(
            AgentEvent.created_at >= _window_start(days),
            AgentEvent.kind == "read",
            AgentEvent.host.isnot(None),
        )
        .group_by(AgentEvent.host)
        .all()
    )
    return {
        host: {
            # Falls back to the row count for reports from an extension that
            # predates the flag. Better slightly high than a panel that says
            # "the reader ran on 0 pages" directly above the responses it read.
            "reports": int(first or 0) or int(seen or 0),
            "json": int(payloads or 0),
            "sent": int(sent or 0),
            "probed": int(probed or 0),
            "url_no": int(url_no or 0),
        }
        for host, first, seen, payloads, sent, probed, url_no in rows
    }


def _is_read(host: str, reading: set[str]) -> bool:
    """Whether the reader is registered on this host, subdomains included."""
    host = (host or "").lower()
    return any(
        host == one or host.endswith(f".{one}") or one.endswith(f".{host}")
        for one in reading
    )


def harvest_health(db, days: int = _MAX_WINDOW_DAYS,
                   pages_days: int | None = None) -> list[dict]:
    """
    Per site: is the harvest still reading this one?

    The failure this exists for is specific and silent. The interceptor reads
    the page's own API responses, so it survives redesigns that would break a
    CSS selector — but not a payload whose field names all change at once. When
    that happens the extension keeps running, keeps forwarding, and keeps
    finding nothing, and the only visible symptom is that a source quietly
    stops contributing.

    A zero is not itself the signal: browsing a feed forwards plenty of
    responses that legitimately contain no jobs, so `found == 0` is a normal
    outcome many times a day. The signal is the *change* — a site that was
    yielding, still has traffic, and now yields nothing.

    So each site is judged against its own last `_RECENT_PAYLOADS` responses
    rather than against a stretch of calendar. That is what makes the panel
    useful on the day it is deployed instead of a week later, and it is why a
    site nobody opened this week is not reported as broken.
    """
    from sqlalchemy import case, func

    from app.models.agent_event import AgentEvent

    found = func.coalesce(AgentEvent.summary["found"].astext.cast(Integer), 0)
    inserted = func.coalesce(AgentEvent.summary["inserted"].astext.cast(Integer), 0)
    merged = func.coalesce(AgentEvent.summary["merged"].astext.cast(Integer), 0)

    # Newest first, per host. Rank 1..25 is "lately"; 26..50 is what it is
    # being compared against.
    rank = func.row_number().over(
        partition_by=AgentEvent.host,
        order_by=AgentEvent.created_at.desc(),
    ).label("nth")

    numbered = (
        db.query(
            AgentEvent.host.label("host"),
            AgentEvent.created_at.label("created_at"),
            found.label("found"),
            inserted.label("inserted"),
            merged.label("merged"),
            rank,
        )
        .filter(
            AgentEvent.created_at >= _window_start(days),
            AgentEvent.kind == "harvest",
            AgentEvent.host.isnot(None),
        )
        .subquery()
    )

    # Pages opened per host, from the other kind of event.
    #
    # Without this a site that was browsed and forwarded *nothing* has no row
    # at all — the query below reads harvest events, and it has none — so it is
    # missing from the panel and indistinguishable from a site nobody opened.
    # That is the exact question "did the JobRight crawl get anything?" asks,
    # and the panel's answer was to omit the site entirely.
    # Counted over the *panel's* window rather than this function's.
    #
    # The two are deliberately different. Payloads are judged against each
    # site's last N responses rather than a stretch of calendar, so that
    # window is long. Pages opened is a plain count, and it sits directly under
    # a heading that says "the last 7 days" and beside a table that totals the
    # same events — so counting it over four months made LinkedIn read as 3,435
    # visits in a week next to a browse total of 854.
    browsed = dict(
        db.query(AgentEvent.host, func.count())
        .filter(
            AgentEvent.created_at >= _window_start(pages_days or days),
            AgentEvent.kind == "browse",
            AgentEvent.host.isnot(None),
        )
        .group_by(AgentEvent.host)
        .all()
    )

    # What the reader looked at, over the same window as the page count. Its
    # own kind of event, because it is reported by pages that forwarded
    # nothing — which is precisely the case with no harvest event to hang it
    # off.
    seen = read_stats(db, pages_days or days)

    is_recent = numbered.c.nth <= _RECENT_PAYLOADS
    rows = (
        db.query(
            numbered.c.host,
            func.count().label("payloads"),
            func.sum(case((is_recent, 1), else_=0)),
            func.sum(case((is_recent, numbered.c.found), else_=0)),
            func.sum(case((is_recent, numbered.c.inserted), else_=0)),
            func.sum(case((is_recent, numbered.c.merged), else_=0)),
            func.sum(case((is_recent, 0), else_=numbered.c.found)),
            func.max(case((numbered.c.found > 0, numbered.c.created_at))),
        )
        # Two windows' worth. Anything older is history, not a comparison.
        .filter(numbered.c.nth <= _RECENT_PAYLOADS * 2)
        .group_by(numbered.c.host)
        .all()
    )

    out = []
    for (host, payloads, recent_payloads, recent_found, recent_inserted,
         recent_merged, earlier_found, last_found_at) in rows:
        recent_payloads = int(recent_payloads or 0)
        recent_found = int(recent_found or 0)
        earlier_found = int(earlier_found or 0)

        if recent_found:
            verdict = "healthy"
        elif recent_payloads < _MIN_PAYLOADS_TO_JUDGE:
            # Too little traffic to distinguish a broken reader from a site
            # nobody opened.
            verdict = "quiet"
        elif earlier_found:
            verdict = "regressed"
        else:
            verdict = "silent"

        out.append(_site_row(host, verdict, payloads, recent_payloads,
                             recent_found, recent_inserted, recent_merged,
                             earlier_found, last_found_at,
                             browsed.pop(host, 0), saw=seen.pop(host, None)))

    # Sites that were opened and sent nothing back. Everything above came from
    # a harvest event, so these have no row there — and being absent reads as
    # "not tried", which is the opposite of what happened.
    #
    # A different problem from `silent`, and a different fix. Silent means the
    # payloads arrive and the reader makes nothing of them, which wants a
    # recipe. This means no payload arrived at all: the interceptor is not
    # registered on that site (its checkbox is off), or the page fetches its
    # jobs from a URL that does not match what the interceptor forwards.
    from app.services import browser_tasks

    try:
        reading = browser_tasks.reading_hosts(db)
    except Exception:
        reading = set()

    # Hosts the reader reported from are included even with no queued browse
    # behind them: most browsing is the user's own, and a site whose reader ran
    # and forwarded nothing is the same fault whoever opened the page.
    for host in sorted(set(browsed) | set(seen)):
        saw = seen.get(host)
        row = _site_row(host, "unread", 0, 0, 0, 0, 0, 0, None,
                        browsed.get(host, 0), saw=saw)
        # The distinction the panel could not draw, and the one that decides
        # what to do about it. "Not enabled" is a checkbox; "enabled and
        # forwarding nothing" is a reader that cannot see the page's requests.
        #
        # A read report settles it outright: the reader cannot have counted
        # responses on a site it was not registered on, so its own evidence
        # outranks the registration list, which describes the browser's
        # configuration *now* rather than at the time of the visit.
        row["enabled"] = bool(saw) or _is_read(host, reading)
        out.append(row)

    # Anything wrong first, then by how much the site is contributing. The
    # panel is read to find a problem, and a regression buried under four
    # working sites is a regression nobody sees.
    order = {"regressed": 0, "unread": 1, "silent": 2, "healthy": 3, "quiet": 4}
    out.sort(key=lambda row: (order[row["verdict"]], -row["found"]))
    return out


def _site_row(host, verdict, payloads, recent_payloads, found, inserted,
              merged, earlier_found, last_found_at, pages, saw=None) -> dict:
    """One site's line on the panel."""
    return {
        "host": host,
        "verdict": verdict,
        "label": HEALTH_LABELS[verdict],
        "payloads": int(payloads or 0),
        "recent_payloads": int(recent_payloads or 0),
        "found": int(found or 0),
        "inserted": int(inserted or 0),
        "merged": int(merged or 0),
        "earlier_found": int(earlier_found or 0),
        "last_found_at": last_found_at,
        # Pages the browser opened here. The denominator the panel was missing:
        # "found 0" means something quite different after sixty visits than
        # after none, and the number was recorded all along in the other event
        # kind without ever being shown next to it.
        "pages": int(pages or 0),
        # Only meaningful on an `unread` row, and set there. True elsewhere
        # because a site that forwarded a payload was self-evidently being
        # read — a row that reported otherwise would be arguing with its own
        # evidence.
        "enabled": True,
        # What the reader looked at here, or None if it never reported — which
        # is itself a finding on an `unread` row: registered but never running.
        "saw": saw,
        "window": _RECENT_PAYLOADS,
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
