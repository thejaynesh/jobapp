from celery import Celery
from celery.schedules import schedule as celery_schedule

from app.config import settings

celery_app = Celery(
    "jobapp",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=[
        "app.tasks.fetch", "app.tasks.match", "app.tasks.generate",
        "app.tasks.backfill", "app.tasks.compare_models", "app.tasks.outreach",
        "app.tasks.interview", "app.tasks.providers", "app.tasks.liveness",
        "app.tasks.descriptions", "app.tasks.links", "app.tasks.enrich",
        "app.tasks.match_eval", "app.tasks.backup", "app.tasks.archive",
        "app.tasks.browse",
    ],
)

VISIBILITY_TIMEOUT_SECONDS = 7200

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    worker_prefetch_multiplier=1,
    # Acknowledge a task when it finishes, not when it is received.
    #
    # With the default (ack on receipt) a worker killed mid-task — a deploy,
    # an OOM, `docker compose up -d` — takes the task with it. Nothing errors
    # and nothing retries; the work simply never happened. That is how a
    # matching pass and the generations queued behind it can both stop dead
    # with no failure recorded anywhere. Late acks put an interrupted task back
    # on the queue instead.
    #
    # The trade is at-least-once delivery: a task can run twice if the worker
    # dies after the work but before the ack. Every task here is safe to repeat
    # — matching re-scores a job, generation rewrites documents — and nothing
    # sends mail unattended, so a duplicate costs time, not a mistake.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # Fail fast when Redis is unreachable. The default connect timeout is long
    # enough that a web request queueing a task — the overlay's "write
    # documents" button, say — reads as a hang rather than as an error. Workers
    # still retry on their own schedule, so a short timeout here just means
    # noticing sooner rather than giving up.
    broker_transport_options={
        "socket_connect_timeout": 3, "socket_timeout": 3,
        # How long Redis waits for a late ack before handing the task to
        # another worker. The default is one hour, and board fetches run for
        # about four — so with late acks every long fetch was re-delivered
        # while still running, which is where the overlapping runs came from.
        #
        # Two hours covers every task's time limit (the longest is 65
        # minutes). The tasks that can legitimately run longer — the fetches,
        # the board backfill, a model comparison — opt out of late acks
        # instead (`acks_late=False` on each): they are rescheduled on their
        # own, so a run lost to a crash costs nothing, and one re-delivered
        # mid-run costs a second copy of hours of requests.
        "visibility_timeout": VISIBILITY_TIMEOUT_SECONDS,
    },
    redis_socket_connect_timeout=3,
    # Two queues, because two kinds of work share this broker and only one of
    # them has somebody waiting on it.
    #
    # Everything used to land in the default queue, and production runs
    # `--concurrency=2`. `match_jobs` and `enrich_jobs` both carry a
    # `soft_time_limit` of 1,500 seconds and both re-queue themselves while
    # there is work, so the two of them can hold both slots for twenty-five
    # minutes at a stretch. A user clicking "Generate documents" then waited
    # behind a backlog pass with nothing but a spinner, and `poll_mailbox` —
    # published every fifteen minutes whether or not the last one ran — piled
    # up behind it.
    #
    # `batch` is the work that is allowed to take half an hour. `interactive`
    # is the work a person is looking at: document generation, the browser
    # agent's queue, and the mailbox. Each gets its own worker (see
    # docker-compose.prod.yml), so a long pass cannot starve a button.
    task_default_queue="interactive",
    task_routes={
        # The dispatcher only reads timestamps and queues work. On the batch
        # queue it would wait behind the multi-hour fetches it is scheduling.
        "app.tasks.fetch.dispatch_due_fetches": {"queue": "interactive"},
        "app.tasks.fetch.*": {"queue": "batch"},
        "app.tasks.match.*": {"queue": "batch"},
        "app.tasks.enrich.*": {"queue": "batch"},
        "app.tasks.archive.*": {"queue": "batch"},
        "app.tasks.backfill.*": {"queue": "batch"},
        "app.tasks.backup.*": {"queue": "batch"},
        "app.tasks.descriptions.*": {"queue": "batch"},
        "app.tasks.liveness.*": {"queue": "batch"},
        "app.tasks.links.*": {"queue": "batch"},
        "app.tasks.match_eval.*": {"queue": "batch"},
        "app.tasks.providers.*": {"queue": "batch"},
        # Deliberately interactive: the user pressed something, or the laptop
        # is waiting for work to do.
        # Pressed on /runs by somebody waiting for the answer. On the batch
        # queue it sat behind hours of fetching and matching, showing "queued,
        # waiting for a worker" until the panel called it stalled.
        "app.tasks.compare_models.*": {"queue": "interactive"},
        "app.tasks.generate.*": {"queue": "interactive"},
        "app.tasks.browse.*": {"queue": "interactive"},
        "app.tasks.outreach.*": {"queue": "interactive"},
        "app.tasks.interview.*": {"queue": "interactive"},
    },
)

# How often beat asks whether a fetch group is due. Short next to any interval
# a person would set, so an hourly group runs within a few minutes of the hour.
FETCH_DISPATCH_TICK_SECONDS = 600

celery_app.conf.beat_schedule = {
    # Fetching in three slices rather than one. The combined task still exists
    # for the manual trigger, but nothing schedules it: one 47-minute cycle
    # meant Adzuna refreshed on the schedule of a Chromium launch, and every
    # posting arrived hours later than it could have.
    # One tick for the three fetch groups rather than a fixed schedule each.
    # A fixed schedule is read once when beat starts, so the intervals on the
    # settings page could not change it; the tick asks each group whether it
    # is due by its *current* setting and queues the ones that are.
    "dispatch-due-fetches": {
        "task": "app.tasks.fetch.dispatch_due_fetches",
        "schedule": celery_schedule(FETCH_DISPATCH_TICK_SECONDS),
    },
    # Boards with a stored credential, asked over their own API. Its own entry
    # rather than a fourth fetch group because it is the one source that can be
    # *unlinked* — a state the fetch cycle has no vocabulary for — and because
    # it needs no browser, which is the entire reason it exists.
    "sweep-linked-boards": {
        "task": "app.tasks.fetch.sweep_linked_boards",
        "schedule": celery_schedule(settings.FETCH_LINKED_INTERVAL_HOURS * 3600),
    },
    # The same board's whole index rather than its feed. Its own entry because
    # it costs a thousand requests to the feed sweep's eleven — one is what you
    # run every few hours, the other is what you run once a day.
    "sweep-linked-boards-deep": {
        "task": "app.tasks.fetch.sweep_linked_boards",
        "schedule": celery_schedule(settings.FETCH_LINKED_DEEP_INTERVAL_HOURS * 3600),
        "kwargs": {"deep": True},
    },
    # Matching used to happen only as a tail-call from a fetch, so a pass that
    # did not finish left jobs sitting as `new` until the next fetch hours
    # later. On its own schedule, unmatched jobs are a delay rather than a
    # dead end. It no-ops in milliseconds when there is nothing new.
    "match-new-jobs": {
        "task": "app.tasks.match.match_jobs",
        "schedule": celery_schedule(settings.MATCH_INTERVAL_MINUTES * 60),
    },
    # Goes back for the descriptions the sources left out. On a schedule of its
    # own as well as a tail-call from each fetch, because the backlog it drains
    # is tens of thousands of jobs stored before enrichment existed — the
    # tail-call only ever sees the jobs that just arrived.
    "enrich-thin-descriptions": {
        "task": "app.tasks.enrich.enrich_jobs",
        "schedule": celery_schedule(settings.ENRICH_INTERVAL_MINUTES * 60),
    },
    # Re-queues generations whose worker died mid-run, and matched jobs whose
    # generation was never queued at all.
    "sweep-stuck-generations": {
        "task": "app.tasks.generate.sweep_generations",
        "schedule": celery_schedule(settings.GENERATION_STUCK_MINUTES * 60),
    },
    # Rewrites documents that were written before enrichment brought the real
    # posting in. Only for applications the user has not acted on — sent is
    # sent, and the file on disk is the record of what the employer received.
    "refresh-stale-documents": {
        "task": "app.tasks.generate.refresh_stale_docs",
        "schedule": celery_schedule(settings.DOC_REFRESH_INTERVAL_HOURS * 3600),
    },
    # Prompts carry whole job descriptions, so the LLM log outgrows everything
    # else in the schema if nothing trims it.
    "prune-llm-log": {
        "task": "app.tasks.providers.prune_llm_log",
        "schedule": celery_schedule(settings.LLM_LOG_PRUNE_INTERVAL_HOURS * 3600),
    },
    # The browser agent's two histories: the event log by row count, and
    # finished browser tasks by age. Nothing pruned the latter at all until
    # there was somewhere else to keep the countable part.
    "prune-agent-history": {
        "task": "app.tasks.providers.prune_agent_history",
        "schedule": celery_schedule(settings.AGENT_EVENT_PRUNE_INTERVAL_HOURS * 3600),
    },
    # Drafts follow-ups that have come due. Drafting only — sending is always a
    # deliberate click, so this never mails anyone on its own.
    "draft-due-outreach-followups": {
        "task": "app.tasks.outreach.process_followups",
        "schedule": celery_schedule(settings.OUTREACH_FOLLOWUP_INTERVAL_HOURS * 3600),
    },
    # A nightly copy of the database, on this machine and nowhere else.
    # Everything else in this file assumes the data survives, and it is the one
    # thing nothing else in the system can recover from.
    "take-backup": {
        "task": "app.tasks.backup.take_backup",
        # Hourly, and the task decides whether one is due — so the interval
        # on the settings page applies without restarting beat.
        "schedule": celery_schedule(3600),
    },
    # Settled rejections stop carrying their descriptions after 60 days. They
    # are moved rather than deleted: deduplication reads three columns off the
    # tombstone, and without them the same posting is re-fetched and re-scored
    # forever.
    "archive-old-jobs": {
        "task": "app.tasks.archive.archive_old_jobs",
        "schedule": celery_schedule(settings.ARCHIVE_INTERVAL_HOURS * 3600),
    },
    # Keeps the browser's queue from running dry. A backstop rather than a
    # fetch cycle: the queue drains at a person's pace, so this tops up only
    # when it is nearly empty and does nothing at all the rest of the time —
    # which is why it can run every half hour and stay cheap. It also declines
    # to queue anything when no agent has polled lately, because work queued
    # for a shut laptop expires unread and hides the real backlog behind it.
    "top-up-browsing": {
        "task": "app.tasks.browse.top_up_browsing",
        "schedule": celery_schedule(settings.BROWSE_TOPUP_INTERVAL_MINUTES * 60),
    },
    "poll-mailbox": {
        "task": "app.tasks.outreach.poll_mailbox",
        "schedule": celery_schedule(settings.IMAP_POLL_INTERVAL_MINUTES * 60),
    },
    # Postings close on the employer's side without telling anyone. Checking
    # the jobs worth applying to keeps "ready to apply" meaning "still open".
    "check-posting-liveness": {
        "task": "app.tasks.liveness.check_postings",
        "schedule": celery_schedule(settings.LIVENESS_INTERVAL_HOURS * 3600),
    },
}


@celery_app.task
def ping():
    return "pong"
