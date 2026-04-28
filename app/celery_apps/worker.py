from celery import Celery

from app.config import CELERY_BROKER_URL, CELERY_RESULT_BACKEND, CELERY_VISIBILITY_TIMEOUT

celery_app = Celery(
    "ev_orchestrator_worker",
    broker=CELERY_BROKER_URL,
    backend=CELERY_RESULT_BACKEND,
    include=["app.celery_apps.tasks.forecast_tasks"],
)

celery_app.conf.update(
    worker_log_level="INFO",
    broker_transport_options={
        "visibility_timeout": CELERY_VISIBILITY_TIMEOUT,
    },
)
