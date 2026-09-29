from celery import Celery
from celery.signals import task_postrun, task_prerun
from celery.schedules import schedule as celery_schedule

from app.config import settings
from app.services import http_pool

# Every worker's requests reuse connections (`services.http_pool`).
http_pool.install()

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
        "app.tasks.browse", "app.tasks.discovery", "app.tasks.sponsorship",
        "app.tasks.recall", "app.tasks.schedule", "app.tasks.tracker",
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
        # The same for everything else on an interval (`tasks.schedule`).
        "app.tasks.schedule.*": {"queue": "interactive"},
        "app.tasks.fetch.*": {"queue": "batch"},
        "app.tasks.match.*": {"queue": "batch"},
        "app.tasks.enrich.*": {"queue": "batch"},
        "app.tasks.archive.*": {"queue": "batch"},
        "app.tasks.backfill.*": {"queue": "batch"},
        "app.tasks.backup.*": {"queue": "batch"},
        "app.tasks.discovery.*": {"queue": "batch"},
        "app.tasks.sponsorship.*": {"queue": "batch"},
        "app.tasks.recall.*": {"queue": "batch"},
        "app.tasks.tracker.*": {"queue": "batch"},
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
    # Everything else on an interval the settings page sets — matching,
    # enrichment, liveness, archiving, the mailbox and the rest. One tick a
    # minute, and each task is sent when its interval, read from the profile
    # on that tick, has passed (`tasks.schedule`). They used to be entries
    # here, which froze each interval at whatever `.env` said when beat
    # started. The reasoning each entry carried is beside it in `SCHEDULE`.
    "dispatch-scheduled": {
        "task": "app.tasks.schedule.dispatch_scheduled",
        "schedule": celery_schedule(60),
    },
    # A nightly copy of the database, on this machine and nowhere else.
    # Everything else in this file assumes the data survives, and it is the one
    # thing nothing else in the system can recover from.
    # Company boards from Common Crawl's URL index — the discovery that does not
    # start from a posting we already hold. Hourly, and the task decides
    # whether a walk is due, so its interval is a setting.
    "discover-boards": {
        "task": "app.tasks.discovery.discover_boards",
        "schedule": celery_schedule(3600),
    },
    # Employers' H-1B filings from DOL's disclosure files. Daily, and a single
    # page read unless DOL has published a quarter since the last run.
    "refresh-h1b-history": {
        "task": "app.tasks.sponsorship.refresh_h1b_history",
        "schedule": celery_schedule(24 * 3600),
    },
    # How much of what SimplifyJobs lists our own readers reach, and what each
    # source finds alone. A database read, once a day.
    "measure-coverage": {
        "task": "app.tasks.recall.measure_coverage",
        "schedule": celery_schedule(24 * 3600),
    },
    "take-backup": {
        "task": "app.tasks.backup.take_backup",
        # Hourly, and the task decides whether one is due — so the interval
        # on the settings page applies without restarting beat.
        "schedule": celery_schedule(3600),
    },
}


# Each task reads the settings page's values at most once, on first use, and
# a value saved there applies from the next task (`tunables.read_once`). Held
# per task id: prerun and postrun are separate calls around the task body.
_SETTINGS_SCOPES: dict = {}


@task_prerun.connect
def _read_settings_once_per_task(task_id=None, **_):
    from app.services import tunables

    scope = tunables.read_once()
    scope.__enter__()
    _SETTINGS_SCOPES[task_id] = scope


@task_postrun.connect
def _release_settings_scope(task_id=None, **_):
    scope = _SETTINGS_SCOPES.pop(task_id, None)
    if scope is not None:
        try:
            scope.__exit__(None, None, None)
        except ValueError:
            # Reset from a different context than it was set in. The value
            # dies with that context anyway; nothing to undo.
            pass


@celery_app.task
def ping():
    return "pong"
