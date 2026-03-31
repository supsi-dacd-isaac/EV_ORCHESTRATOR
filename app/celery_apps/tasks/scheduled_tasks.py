import os
from datetime import timedelta

from celery.schedules import crontab, schedule as interval_schedule
from celery.utils.log import get_task_logger
from redbeat import RedBeatSchedulerEntry

from app.celery_apps.scheduler import scheduler_app
from app.celery_apps.task_names import GENERATE_PILOT_FORECAST
from app.services.ev_forecast.forecast_manifest import load_forecast_jobs

logger = get_task_logger(__name__)


@scheduler_app.task(queue="scheduler")
def check_and_schedule_tasks() -> None:
    jobs = load_forecast_jobs()
    active_ids = {j.id for j in jobs if j.enabled and j.predict_periodicity_minutes > 0}

    for job in jobs:
        entry_name = f"ev_forecast_job_{job.id}"
        if not job.enabled or job.predict_periodicity_minutes <= 0:
            try:
                entry = RedBeatSchedulerEntry.from_key(f"redbeat:{entry_name}", app=scheduler_app)
                entry.delete()
                logger.info("Removed RedBeat entry %s", entry_name)
            except KeyError:
                pass
            continue

        p = max(1, int(job.predict_periodicity_minutes))
        if p >= 60 and p % 60 == 0:
            schedule = crontab(minute="0", hour=f"*/{p // 60}")
        else:
            schedule = interval_schedule(run_every=timedelta(minutes=p))
        entry = RedBeatSchedulerEntry(
            name=entry_name,
            task=GENERATE_PILOT_FORECAST,
            schedule=schedule,
            args=(job.id,),
            app=scheduler_app,
            options={"queue": "celery"},
        )
        entry.save()
        logger.debug("Synced RedBeat entry %s every %s min", entry_name, job.predict_periodicity_minutes)

    if os.getenv("FORECAST_PRUNE_ORPHAN_REDBEAT", "").lower() in ("1", "true", "yes"):
        _prune_orphan_redbeat(active_ids)


def _prune_orphan_redbeat(active_ids: set[str]) -> None:
    import redis

    url = os.getenv("REDBEAT_REDIS_URL", "redis://localhost:6379/0")
    client = redis.StrictRedis.from_url(url)
    prefix = b"redbeat:ev_forecast_job_"
    for key in client.scan_iter(match="redbeat:ev_forecast_job_*"):
        if not key.startswith(prefix):
            continue
        jid = key.decode("utf-8").removeprefix("redbeat:ev_forecast_job_")
        if jid not in active_ids:
            client.delete(key)
            logger.info("Pruned orphan RedBeat key %s", key.decode("utf-8", errors="replace"))
