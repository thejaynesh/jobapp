from celery import Celery
from celery.schedules import schedule as celery_schedule

from app.config import settings

celery_app = Celery(
    "jobapp",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=["app.tasks.fetch"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    worker_prefetch_multiplier=1,
)

celery_app.conf.beat_schedule = {
    "fetch-jobs-every-5-hours": {
        "task": "app.tasks.fetch.fetch_jobs",
        "schedule": celery_schedule(settings.FETCH_INTERVAL_HOURS * 3600),
    },
}


@celery_app.task
def ping():
    return "pong"
