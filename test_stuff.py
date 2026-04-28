from app.db.session import SessionLocal
from app.services.ev_forecast.forecast_manifest import get_job_by_id
from app.services.ev_forecast.celery_pipeline import run_forecast_job
db = SessionLocal()
try:
    job = get_job_by_id("example_pilot_forecast")
    if job is None:
        raise SystemExit("Job id not found in YAML")
    out = run_forecast_job(db, job)
    print(out)
finally:
    db.close()