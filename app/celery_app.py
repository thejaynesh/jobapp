from celery import Celery
from app.config import settings

celery_app = Celery(
    "jobapp",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=[],  # task modules registered here as plans build them
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

celery_app.conf.beat_schedule = {}


@celery_app.task
def ping():
    return "pong"
