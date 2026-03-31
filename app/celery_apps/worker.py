import os

from celery import Celery

celery_app = Celery(
    "ev_orchestrator_worker",
    broker=os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0"),
    backend=os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/0"),
    include=["app.celery_apps.tasks.forecast_tasks"],
)

celery_app.conf.update(
    worker_log_level="INFO",
    broker_transport_options={
        "visibility_timeout": int(os.getenv("CELERY_VISIBILITY_TIMEOUT", "43200")),
    },
)
