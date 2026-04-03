import os

import redis
from celery import Celery
from celery.schedules import crontab
from celery.utils.log import get_task_logger

logger = get_task_logger(__name__)

scheduler_app = Celery(
    "ev_orchestrator_scheduler",
    broker=os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0"),
    backend=os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/0"),
    include=["app.celery_apps.tasks.scheduled_tasks"],
)

scheduler_app.conf.update(
    timezone="UTC",
    task_routes={
        "app.celery_apps.tasks.scheduled_tasks.check_and_schedule_tasks": {
            "queue": "scheduler",
        },
    },
    beat_schedule={
        "check-forecast-manifest": {
            "task": "app.celery_apps.tasks.scheduled_tasks.check_and_schedule_tasks",
            "schedule": crontab(minute="*/1"),
        },
    },
    beat_scheduler="redbeat.RedBeatScheduler",
    redbeat_redis_url=os.getenv("REDBEAT_REDIS_URL", "redis://localhost:6379/0"),
    redbeat_lock_key="redbeat::lock",
    redbeat_lock_timeout=int(os.getenv("REDBEAT_LOCK_TIMEOUT", "300")),
    broker_transport_options={
        "visibility_timeout": int(os.getenv("CELERY_VISIBILITY_TIMEOUT", "43200")),
    },
)


def _purge_redbeat_and_broker() -> None:
    redis_url = os.getenv("REDBEAT_REDIS_URL", "redis://localhost:6379/0")
    redis_client = redis.StrictRedis.from_url(redis_url)
    for key in redis_client.keys("redbeat:*"):
        logger.info("Deleting %s", key.decode("utf-8", errors="replace"))
        redis_client.delete(key)
    logger.info("RedBeat keys cleared.")
    scheduler_app.control.purge()


if os.getenv("CELERY_PURGE_ON_START", "").lower() in ("1", "true", "yes"):
    logger.warning("CELERY_PURGE_ON_START set — purging RedBeat and broker queues")
    _purge_redbeat_and_broker()
