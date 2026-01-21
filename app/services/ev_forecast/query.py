from typing import Tuple
from datetime import datetime
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models.db.ev_forecast_stats import EVForecastStatsDB
from app.services.common.constants import GENERIC_CHARGER_ID

def get_ev_forecast(charger_id: str, start_time: datetime) -> Tuple[float, float, float, float]:
    """
    Forecast EV charging energy and duration
    Returns:
    - forecasted_energy_kwh
    - std_energy_kwh
    - forecasted_duration_hours
    - std_duration_hours
    """

    connection_hour = int(start_time.hour)
    db: Session = SessionLocal()

    try:
        # Try charger-specific forecast
        stat = (
            db.query(EVForecastStatsDB)
            .filter(
                EVForecastStatsDB.charger_id == charger_id,
                EVForecastStatsDB.hour == connection_hour
            )
            .first()
        )

        if stat is None:
            stat = (
                db.query(EVForecastStatsDB).filter(EVForecastStatsDB.charger_id == GENERIC_CHARGER_ID,
                                                   EVForecastStatsDB.hour == connection_hour).first()
            )

        if stat is None:
            return 0.0, 0.0, 0.0, 0.0

        return stat.mean_energy_kwh, stat.std_energy_kwh, stat.mean_duration_hours, stat.std_duration_hours

    finally:
        db.close()
