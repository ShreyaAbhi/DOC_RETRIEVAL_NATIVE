from celery import Celery
from celery.schedules import crontab
from app.core.config import settings

celery_app = Celery(
    "pod_worker",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=["app.core.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    # One task at a time so Ollama calls never run in parallel
    worker_concurrency=1,
    # Default queue matches the worker's -Q pod_tasks flag
    task_default_queue="pod_tasks",
    # Re-queue tasks if the worker crashes mid-run
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    beat_schedule={
        'poll-ftp-for-pods': {
            'task': 'app.core.tasks.poll_ftp_task',
            'schedule': 1800.0,
        },
        'check-ollama-health': {
            'task': 'app.core.tasks.check_ollama_task',
            'schedule': 21600.0,  # every 6 hours
        },
        'hard-delete-retention': {
            'task': 'app.core.tasks.hard_delete_retention_task',
            'schedule': crontab(hour=2, minute=0),  # daily at 02:00 UTC
        },
    },
)
