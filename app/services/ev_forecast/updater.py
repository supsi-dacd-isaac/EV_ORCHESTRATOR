from datetime import datetime
from math import sqrt
from sqlalchemy.orm import Session
from sqlalchemy import extract
import numpy as np

from app.models.db.ev_forecast_stats import EVForecastStatsDB
from app.services.common.db_utils import get_db_session, completed_sessions_count
from app.services.common.constants import GENERIC_CHARGER_ID, MIN_SESSIONS_FOR_CHARGER_SPECIFIC
from app.models.db.charging_session import ChargingSessionDB

def _update_online_stats(mean: float, var: float, n: int, x: float):
    n_new = n + 1
    delta = x - mean
    mean_new = mean + delta / n_new
    delta2 = x - mean_new
    var_new = var + delta * delta2
    return mean_new, var_new, n_new


def update_ev_forecast(
    charger_id: str,
    start_time: datetime,
    energy_kwh: float,
    duration_hours: float,
):
    """
    Update EV forecast statistics after a completed charging session.
    """

    hour = start_time.hour

    with get_db_session() as db:

        # -----------------------------------
        # Always update GENERIC forecast
        # -----------------------------------
        _update_or_initialize_stat(
            db,
            charger_id=GENERIC_CHARGER_ID,
            hour=hour,
            energy_kwh=energy_kwh,
            duration_hours=duration_hours,
        )

        # -----------------------------------
        # Charger-specific logic
        # -----------------------------------
        charger_stat = (
            db.query(EVForecastStatsDB)
            .filter(
                EVForecastStatsDB.charger_id == charger_id,
                EVForecastStatsDB.hour == hour,
            )
            .first()
        )

        if charger_stat:
            _update_existing_stat(
                charger_stat,
                energy_kwh,
                duration_hours,
            )

        else:
            sessions = (
                db.query(ChargingSessionDB)
                .filter(
                    ChargingSessionDB.charger_id == charger_id,
                    extract("hour", ChargingSessionDB.start_time) == hour,
                )
                .all()
            )

            count = len(sessions)
            if count >= MIN_SESSIONS_FOR_CHARGER_SPECIFIC:
                _initialize_new_stat(
                    db,
                    charger_id,
                    hour,
                    sessions
                )



def _update_or_initialize_stat(
    db: Session,
    charger_id: str,
    hour: int,
    energy_kwh: float,
    duration_hours: float,
):
    stat = (
        db.query(EVForecastStatsDB)
        .filter(
            EVForecastStatsDB.charger_id == charger_id,
            EVForecastStatsDB.hour == hour,
        )
        .first()
    )

    if stat is None:
        stat = EVForecastStatsDB(
            charger_id=charger_id,
            hour=hour,
            mean_energy_kwh=energy_kwh,
            std_energy_kwh=0.0,
            mean_duration_hours=duration_hours,
            std_duration_hours=0.0,
            sample_count=1,
            updated_at=datetime.utcnow(),
        )
        db.add(stat)
        return

    _update_existing_stat(stat, energy_kwh, duration_hours)


def _update_existing_stat(
    stat: EVForecastStatsDB,
    energy_kwh: float,
    duration_hours: float,
):
    energy_var = (stat.std_energy_kwh ** 2) * stat.sample_count
    duration_var = (stat.std_duration_hours ** 2) * stat.sample_count

    mean_energy, energy_var, n = _update_online_stats(
        stat.mean_energy_kwh, energy_var, stat.sample_count, energy_kwh
    )
    mean_duration, duration_var, _ = _update_online_stats(
        stat.mean_duration_hours, duration_var, stat.sample_count, duration_hours
    )

    stat.mean_energy_kwh = mean_energy
    stat.std_energy_kwh = sqrt(energy_var / n)
    stat.mean_duration_hours = mean_duration
    stat.std_duration_hours = sqrt(duration_var / n)
    stat.sample_count = n
    stat.updated_at = datetime.utcnow()


def _initialize_new_stat(
    db: Session,
    charger_id: str,
    hour: int,
    sessions: list[ChargingSessionDB],
):
    energy_values = [s.energy_delivered_kwh for s in sessions]
    duration_values = [(s.end_charging_time - s.start_time).total_seconds() / 3600 for s in sessions]

    mean_energy = float(np.mean(energy_values))
    std_energy = float(np.std(energy_values, ddof=0))
    mean_duration = float(np.mean(duration_values))
    std_duration = float(np.std(duration_values, ddof=0))
    sample_count = len(sessions)

    stat = EVForecastStatsDB(
        charger_id=charger_id,
        hour=hour,
        mean_energy_kwh=mean_energy,
        std_energy_kwh=std_energy,
        mean_duration_hours=mean_duration,
        std_duration_hours=std_duration,
        sample_count= sample_count,
        updated_at=datetime.utcnow(),
    )
    db.add(stat)
