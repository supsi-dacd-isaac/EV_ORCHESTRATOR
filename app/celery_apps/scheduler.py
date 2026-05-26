import redis
from celery import Celery
from celery.schedules import crontab
from celery.utils.log import get_task_logger

from app.config import (
    CELERY_BROKER_URL,
    CELERY_PURGE_ON_START,
    CELERY_RESULT_BACKEND,
    CELERY_VISIBILITY_TIMEOUT,
    REDBEAT_LOCK_TIMEOUT,
    REDBEAT_REDIS_URL,
)

logger = get_task_logger(__name__)

scheduler_app = Celery(
    "ev_orchestrator_scheduler",
    broker=CELERY_BROKER_URL,
    backend=CELERY_RESULT_BACKEND,
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
    beat_max_loop_interval=60,
    beat_scheduler="redbeat.RedBeatScheduler",
    redbeat_redis_url=REDBEAT_REDIS_URL,
    redbeat_lock_key="redbeat::lock",
    redbeat_lock_timeout=REDBEAT_LOCK_TIMEOUT,
    broker_transport_options={
        "visibility_timeout": CELERY_VISIBILITY_TIMEOUT,
    },
)


def _purge_redbeat_and_broker() -> None:
    redis_client = redis.StrictRedis.from_url(REDBEAT_REDIS_URL)
    for key in redis_client.keys("redbeat:*"):
        logger.info("Deleting %s", key.decode("utf-8", errors="replace"))
        redis_client.delete(key)
    logger.info("RedBeat keys cleared.")
    scheduler_app.control.purge()


if CELERY_PURGE_ON_START:
    logger.warning("CELERY_PURGE_ON_START set — purging RedBeat and broker queues")
    _purge_redbeat_and_broker()
