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
        "app.tasks.interview", "app.tasks.providers",
    ],
)

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
    broker_transport_options={"socket_connect_timeout": 3, "socket_timeout": 3},
    redis_socket_connect_timeout=3,
)

celery_app.conf.beat_schedule = {
    "fetch-jobs-every-5-hours": {
        "task": "app.tasks.fetch.fetch_jobs",
        "schedule": celery_schedule(settings.FETCH_INTERVAL_HOURS * 3600),
    },
    # Matching used to happen only as a tail-call from a fetch, so a pass that
    # did not finish left jobs sitting as `new` until the next fetch hours
    # later. On its own schedule, unmatched jobs are a delay rather than a
    # dead end. It no-ops in milliseconds when there is nothing new.
    "match-new-jobs": {
        "task": "app.tasks.match.match_jobs",
        "schedule": celery_schedule(settings.MATCH_INTERVAL_MINUTES * 60),
    },
    # Re-queues generations whose worker died mid-run, and matched jobs whose
    # generation was never queued at all.
    "sweep-stuck-generations": {
        "task": "app.tasks.generate.sweep_generations",
        "schedule": celery_schedule(settings.GENERATION_STUCK_MINUTES * 60),
    },
    # Prompts carry whole job descriptions, so the LLM log outgrows everything
    # else in the schema if nothing trims it.
    "prune-llm-log": {
        "task": "app.tasks.providers.prune_llm_log",
        "schedule": celery_schedule(settings.LLM_LOG_PRUNE_INTERVAL_HOURS * 3600),
    },
    # Drafts follow-ups that have come due. Drafting only — sending is always a
    # deliberate click, so this never mails anyone on its own.
    "draft-due-outreach-followups": {
        "task": "app.tasks.outreach.process_followups",
        "schedule": celery_schedule(settings.OUTREACH_FOLLOWUP_INTERVAL_HOURS * 3600),
    },
    "poll-mailbox": {
        "task": "app.tasks.outreach.poll_mailbox",
        "schedule": celery_schedule(settings.IMAP_POLL_INTERVAL_MINUTES * 60),
    },
}


@celery_app.task
def ping():
    return "pong"
