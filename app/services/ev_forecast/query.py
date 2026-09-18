from typing import Tuple
from datetime import datetime
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models import EvForecastStats
from app.services.common.constants import GENERIC_CHARGER_ID
from app.services.common.db_utils import get_pilot_tz_for_charger, get_local_hour, to_utc


def get_ev_forecast(charger_id: str, start_time: datetime) -> Tuple[float, float, float, float]:
    """
    Forecast EV charging energy and duration
    Returns:
    - forecasted_energy_kwh
    - std_energy_kwh
    - forecasted_duration_hours
    - std_duration_hours

    Uses the latest stored EvForecastStats row (by updated_at) for the given charger/local_hour.
    Falls back to GENERIC_CHARGER_ID if charger-specific stats are not present.

    ``start_time`` uses the same convention as event handling: naive datetimes are
    pilot-local wall clock (via ``to_utc(..., local_tz=pilot_tz)``), so London and
    Zurich both map naive 08:09 to generic ``local_hour=8``.
    """

    db: Session = SessionLocal()

    try:
        tz_name = get_pilot_tz_for_charger(db, charger_id)
        # Align with session.start_time = to_utc(event.timestamp, local_tz=pilot).
        start_utc = to_utc(start_time, local_tz=tz_name)
        connection_local_hour = get_local_hour(start_utc, tz_name)

        # Try charger-specific forecast
        stat = (
            db.query(EvForecastStats)
            .filter(
                EvForecastStats.id_charger == charger_id,
                EvForecastStats.local_hour == connection_local_hour,
            )
            .order_by(EvForecastStats.updated_at.desc())
            .first()
        )

        # Fallback to generic
        if stat is None:
            stat = (
                db.query(EvForecastStats)
                .filter(
                    EvForecastStats.id_charger == GENERIC_CHARGER_ID,
                    EvForecastStats.local_hour == connection_local_hour,
                )
                .order_by(EvForecastStats.updated_at.desc())
                .first()
            )

        if stat is None:
            return 10.0, 0.0, 2.0, 0.0 #TODO : assign realistic default values

        return stat.mean_energy_kwh, stat.std_energy_kwh, stat.mean_duration_hours, stat.std_duration_hours

    finally:
        db.close()
