"""
Work that runs on an interval the settings page sets.

Beat reads its schedule once, when it starts. An entry there saying "every
`settings.LIVENESS_INTERVAL_HOURS` hours" froze whatever the environment said
at that moment, so the interval could only ever be changed by editing `.env`
and redeploying — and a field for it on the settings page would have saved
without error and changed nothing.

So these tick every minute instead. Each tick reads the profile once, and
sends each task whose interval, as currently set, has passed since it was last
sent. `fetch.dispatch_due_fetches` does the same for the three fetch groups,
which carry extra rules of their own (locks, a queued marker).

When a task was last sent is kept in Redis, which is also what the tasks are
sent through: a tick that cannot reach it could not have sent anything either.
A task with no record is due — the first tick after a deploy of this sends
each one once, where beat would have waited a whole interval, and with daily
entries and several deploys a day that wait was how a daily task could go
days without running.
"""

import logging
import time
from dataclasses import dataclass, field

from app.celery_app import celery_app

logger = logging.getLogger(__name__)

# Short next to any interval a person would set, so a fifteen-minute poll runs
# within a minute of when it is due.
TICK_SECONDS = 60

# A task due at 14:00 that the tick reaches at 13:59:40 should go now, not a
# minute late every time.
_SLACK_SECONDS = 30

_KEY = "jobapp:schedule:sent:{name}"


@dataclass(frozen=True)
class Every:
    """One scheduled task: `task` every `tunable` × `unit` seconds."""

    name: str
    task: str
    tunable: str
    unit: int
    kwargs: dict = field(default_factory=dict)


HOUR, MINUTE = 3600, 60

SCHEDULE: tuple[Every, ...] = (
    Every("retry-browser-ingestion", "app.tasks.browse.retry_ingestion",
          "agent_ingest_retry_minutes", MINUTE),
    # Boards with a stored credential, asked over their own API — the feed,
    # then the whole index less often (a thousand requests to the feed's eleven).
    Every("sweep-linked-boards", "app.tasks.fetch.sweep_linked_boards",
          "fetch_linked_interval_hours", HOUR),
    Every("sweep-linked-boards-deep", "app.tasks.fetch.sweep_linked_boards",
          "fetch_linked_deep_interval_hours", HOUR, {"deep": True}),
    # Matching on its own clock, so "still new" is always temporary. It no-ops
    # in milliseconds when there is nothing new.
    Every("match-new-jobs", "app.tasks.match.match_jobs", "match_interval_minutes", MINUTE),
    Every("enrich-thin-descriptions", "app.tasks.enrich.enrich_jobs",
          "enrich_interval_minutes", MINUTE),
    # Generations whose worker died mid-run, looked for as often as one counts
    # as stuck.
    Every("sweep-stuck-generations", "app.tasks.generate.sweep_generations",
          "generation_stuck_minutes", MINUTE),
    # Documents written before enrichment brought the real posting in, for
    # applications the user has not acted on.
    Every("refresh-stale-documents", "app.tasks.generate.refresh_stale_docs",
          "doc_refresh_interval_hours", HOUR),
    # Prompts carry whole job descriptions, so the LLM log outgrows everything
    # else in the schema if nothing trims it.
    Every("prune-llm-log", "app.tasks.providers.prune_llm_log",
          "llm_log_prune_interval_hours", HOUR),
    # The browser agent's two histories: the event log by row count, and
    # finished browser tasks by age.
    Every("prune-agent-history", "app.tasks.providers.prune_agent_history",
          "agent_event_prune_interval_hours", HOUR),
    # Drafting only — sending is always a deliberate click.
    Every("draft-due-outreach-followups", "app.tasks.outreach.process_followups",
          "outreach_followup_interval_hours", HOUR),
    # Settled rejections stop carrying their descriptions. Moved rather than
    # deleted: deduplication reads three columns off the tombstone, and without
    # them the same posting is re-fetched and re-scored forever.
    Every("archive-old-jobs", "app.tasks.archive.archive_old_jobs",
          "archive_interval_hours", HOUR),
    # Does nothing unless the browser's queue is nearly empty, so the interval
    # decides responsiveness rather than volume.
    Every("top-up-browsing", "app.tasks.browse.top_up_browsing",
          "browse_topup_interval_minutes", MINUTE),
    Every("poll-mailbox", "app.tasks.outreach.poll_mailbox",
          "imap_poll_interval_minutes", MINUTE),
    # Postings close on the employer's side without telling anyone.
    Every("check-posting-liveness", "app.tasks.liveness.check_postings",
          "liveness_interval_hours", HOUR),
    # How often matching agreed with what you did, and any better minimum
    # score, as a line in the log. The report page is always current.
    Every("report-matching", "app.tasks.match_eval.report_matching",
          "match_report_interval_hours", HOUR),
    # The ranking learned from applications and dismissals, retrained so new
    # decisions count.
    Every("retrain-for-you", "app.tasks.match_eval.retrain_for_you",
          "for_you_retrain_hours", HOUR),
    # Next actions on applications that have come due, as one line in the log.
    Every("remind-due-actions", "app.tasks.tracker.remind_due_actions",
          "reminder_interval_hours", HOUR),
)


def _client():
    from app.services.fetch_lock import _client as redis_client

    return redis_client()


def interval_seconds(entry: Every, profile_data: dict | None) -> float:
    """The entry's interval as the settings page has it now."""
    from app.services.tunables import value

    try:
        return max(1.0, float(value(profile_data, entry.tunable))) * entry.unit
    except (TypeError, ValueError):
        return float(entry.unit)


def due(entry: Every, profile_data: dict | None, now: float, redis) -> bool:
    """Whether `entry`'s interval has passed since it was last sent."""
    raw = redis.get(_KEY.format(name=entry.name))
    if raw is None:
        return True
    try:
        last = float(raw)
    except (TypeError, ValueError):
        return True
    return now - last >= interval_seconds(entry, profile_data) - _SLACK_SECONDS


@celery_app.task(name="app.tasks.schedule.dispatch_scheduled", max_retries=0, acks_late=False)
def dispatch_scheduled() -> list[str]:
    """Send every scheduled task whose interval has passed. Returns their names."""
    from app.services.tunables import _load_profile_data

    profile_data = _load_profile_data()
    redis = _client()
    now = time.time()
    sent = []
    for entry in SCHEDULE:
        try:
            if not due(entry, profile_data, now, redis):
                continue
            celery_app.send_task(entry.task, kwargs=dict(entry.kwargs))
            # Kept for three intervals: long enough to answer the next tick,
            # and a task taken out of this table leaves nothing behind.
            redis.set(_KEY.format(name=entry.name), str(now),
                      ex=int(interval_seconds(entry, profile_data) * 3) + TICK_SECONDS)
            sent.append(entry.name)
        except Exception as exc:
            # One entry's failure is that entry's: the rest still go.
            logger.warning("schedule: could not dispatch %s: %s", entry.name, exc)
    if sent:
        logger.info("schedule: sent %s", ", ".join(sent))
    return sent


BY_NAME: dict[str, Every] = {entry.name: entry for entry in SCHEDULE}
