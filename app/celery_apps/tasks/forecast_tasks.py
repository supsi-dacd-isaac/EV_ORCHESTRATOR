from celery.utils.log import get_task_logger

from app.celery_apps.task_names import GENERATE_PILOT_FORECAST
from app.celery_apps.worker import celery_app
from app.db.session import SessionLocal
from app.services.ev_forecast.celery_pipeline import run_forecast_job
from app.services.ev_forecast.forecast_manifest import get_job_by_id

logger = get_task_logger(__name__)


@celery_app.task(name=GENERATE_PILOT_FORECAST)
def generate_pilot_forecast(job_id: str) -> dict:
    job = get_job_by_id(job_id)
    if job is None:
        logger.warning("Unknown forecast job_id=%s", job_id)
        return {"status": "error", "reason": "unknown_job", "rows": 0}
    if not job.enabled:
        logger.info("Job %s disabled; skip", job_id)
        return {"status": "skipped", "reason": "disabled", "rows": 0}

    db = SessionLocal()
    try:
        result = run_forecast_job(db, job)
        logger.info("Forecast job %s: %s", job_id, result)
        return result
    except Exception:
        logger.exception("Forecast job %s failed", job_id)
        db.rollback()
        raise
    finally:
        db.close()
