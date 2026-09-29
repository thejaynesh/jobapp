"""
Where work is, between fetching a job and having documents for it.

Every stage here already reported itself somewhere — a log line in a container,
a status column on a row, a list length in Redis — which is another way of
saying none of them reported anywhere anyone looks. The symptom that produced
this module was "matching seems stuck and matched jobs never generate", and
answering it needed a shell, three commands and the schema. It should need a
page refresh.

The counts are deliberately blunt: how many are waiting, how many are running,
how many failed and why. A stage that is slow and a stage that is dead look the
same in a snapshot, so the oldest waiting item is reported too — that is the
number that separates them.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import func

from app.config import live, settings

logger = logging.getLogger(__name__)

# The stage each status belongs to, in the order work moves through them.
JOB_STAGES = [
    ("new", "Waiting to be scored"),
    ("matched", "Matched"),
    ("docs_generated", "Documents written"),
    ("filtered_out", "Filtered out"),
]

GENERATION_STAGES = [
    ("idle", "Not started"),
    ("generating", "Writing"),
    ("done", "Done"),
    ("failed", "Failed"),
]


def _age_minutes(when: datetime | None) -> int | None:
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - when).total_seconds() // 60))


def queue_depth() -> dict:
    """
    How much is waiting on the broker, and how much a worker has claimed.

    An empty queue with work outstanding is the interesting case: it means
    nothing is coming to do that work, which is what a dropped task looks like
    from the outside.
    """
    try:
        import redis

        from app.celery_app import celery_app
        from kombu.transport.redis import Channel

        options = celery_app.conf.broker_transport_options or {}
        priority_steps = options.get("priority_steps", Channel.priority_steps)
        separator = options.get("sep", Channel.sep)
        prefix = options.get("global_keyprefix", "")
        names = {celery_app.conf.task_default_queue}
        names.update(route["queue"] for route in celery_app.conf.task_routes.values()
                     if isinstance(route, dict) and route.get("queue"))
        names.update(celery_app.amqp.queues)
        with redis.Redis.from_url(settings.REDIS_URL, socket_connect_timeout=3,
                                  socket_timeout=3) as client:
            with client.pipeline(transaction=False) as pipe:
                for name in sorted(names):
                    for priority in priority_steps:
                        suffix = f"{separator}{priority}" if priority else ""
                        pipe.llen(f"{prefix}{name}{suffix}")
                pipe.hlen(f"{prefix}{options.get('unacked_key', Channel.unacked_key)}")
                values = pipe.execute()
        width = len(priority_steps)
        queues = {name: sum(values[index * width:(index + 1) * width])
                  for index, name in enumerate(sorted(names))}
        return {"waiting": sum(queues.values()), "queues": queues,
                "claimed": int(values[-1]), "reachable": True}
    except Exception as exc:
        logger.warning("pipeline: queue depth unavailable: %s", exc)
        return {"waiting": None, "claimed": None, "reachable": False, "error": str(exc)}


def status(db) -> dict:
    """The whole pipeline in one read, for the panel on /runs."""
    from app.models.application import Application
    from app.models.job import Job, JobStatus

    job_counts = dict(
        (row[0].value if hasattr(row[0], "value") else row[0], row[1])
        for row in db.query(Job.status, func.count(Job.id)).group_by(Job.status).all()
    )
    generation_counts = dict(
        db.query(Application.generation_status, func.count(Application.id))
        .group_by(Application.generation_status)
        .all()
    )

    oldest_unmatched = (
        db.query(func.min(Job.fetched_at)).filter(Job.status == JobStatus.new).scalar()
    )
    oldest_generating = (
        db.query(func.min(Application.generation_started_at))
        .filter(Application.generation_status == "generating")
        .scalar()
    )

    # The errors themselves, not just the count. A failure whose reason lives
    # only in a worker log is a failure nobody acts on.
    failures = [
        {
            "id": str(app.id),
            "company": app.job.company if app.job else "",
            "title": app.job.title if app.job else "",
            "error": (app.generation_error or "")[:200],
        }
        for app in (
            db.query(Application)
            .filter(Application.generation_status == "failed")
            .order_by(Application.created_at.desc())
            .limit(5)
            .all()
        )
    ]

    from app.services.fetch_lock import state
    from app.tasks.match import MATCH_LOCK_KEY

    waiting_to_score = job_counts.get("new", 0)
    stuck_minutes = _age_minutes(oldest_generating)
    return {
        "jobs": [
            {"key": key, "label": label, "count": job_counts.get(key, 0)}
            for key, label in JOB_STAGES
        ],
        "generation": [
            {"key": key, "label": label, "count": generation_counts.get(key, 0)}
            for key, label in GENERATION_STAGES
        ],
        "waiting_to_score": waiting_to_score,
        "oldest_unmatched_minutes": _age_minutes(oldest_unmatched),
        "oldest_generating_minutes": stuck_minutes,
        # Past the sweeper's threshold, so it is a stall rather than a wait.
        "generation_stalled": (
            stuck_minutes is not None and stuck_minutes >= live().GENERATION_STUCK_MINUTES
        ),
        "matching_now": state(key=MATCH_LOCK_KEY).get("running", False),
        "match_interval_minutes": live().MATCH_INTERVAL_MINUTES,
        "match_batch_size": live().MATCH_MAX_JOBS_PER_TASK,
        "failures": failures,
        "queue": queue_depth(),
    }
