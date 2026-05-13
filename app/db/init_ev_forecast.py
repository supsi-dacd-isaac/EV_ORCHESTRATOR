import uuid
from app.db.session import SessionLocal
from app.models import EvForecastStats
from app.db.ev_forecast_defaults import DEFAULT_EV_FORECAST


def init_ev_forecast(charger_id: str):
    """
    Initialize EV forecast stats for a specific charger in the database.
    """
    db = SessionLocal()
    try:
        for hour, (mean_energy, std_energy, mean_duration, std_duration, sample_count) in DEFAULT_EV_FORECAST.items():
            stat = EvForecastStats(
                id=uuid.uuid4(),
                id_charger=charger_id,
                local_hour=hour,
                mean_energy_kwh=mean_energy,
                std_energy_kwh=std_energy,
                mean_duration_hours=mean_duration,
                std_duration_hours=std_duration,
                sample_count=sample_count,
            )
            db.add(stat)
        db.commit()
        print(f"Initialized EV forecast stats for charger_id='{charger_id}'")
    except Exception as e:
        db.rollback()
        print("Failed to initialize EV forecast stats:", e)
    finally:
        db.close()