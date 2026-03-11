from datetime import datetime
from math import sqrt
from sqlalchemy.orm import Session
from sqlalchemy import extract
import numpy as np
from uuid import uuid4

from app.models import EvForecastStats
from app.services.common.db_utils import get_db_session, completed_sessions_count
from app.services.common.constants import GENERIC_CHARGER_ID, MIN_SESSIONS_FOR_CHARGER_SPECIFIC
from app.models import ChargingSessions


def _update_online_stats(mean: float, var: float, n: int, x: float):
    n_new = n + 1
    delta = x - mean
    mean_new = mean + delta / n_new
    delta2 = x - mean_new
    var_new = var + delta * delta2
    return mean_new, var_new, n_new


def update_ev_forecast(
    db: Session,
    charger_id: str,
    start_time: datetime,
    energy_kwh: float,
    duration_hours: float,
):
    """
    Update EV forecast statistics after a completed charging session.
    """

    hour = start_time.hour

    # -----------------------------------
    # Always update GENERIC forecast (append new row)
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
        db.query(EvForecastStats).filter(
            EvForecastStats.id_charger == charger_id,
            EvForecastStats.hour == hour,
        )
        .order_by(EvForecastStats.updated_at.desc())
        .first()
    )

    if charger_stat:
        # derive new aggregated stats from latest
        energy_var = (charger_stat.std_energy_kwh ** 2) * charger_stat.sample_count
        duration_var = (charger_stat.std_duration_hours ** 2) * charger_stat.sample_count

        mean_energy, energy_var, n = _update_online_stats(
            charger_stat.mean_energy_kwh, energy_var, charger_stat.sample_count, energy_kwh
        )
        mean_duration, duration_var, _ = _update_online_stats(
            charger_stat.mean_duration_hours, duration_var, charger_stat.sample_count, duration_hours
        )

        new_stat = EvForecastStats(
            id=uuid4(),
            hour=hour,
            mean_energy_kwh=mean_energy,
            std_energy_kwh=sqrt(energy_var / n),
            mean_duration_hours=mean_duration,
            std_duration_hours=sqrt(duration_var / n),
            sample_count=n,
            id_charger=charger_id,
        )
        db.add(new_stat)

    else:
        # check sessions to decide whether to initialize a new charger-specific stat
        sessions = (
                db.query(ChargingSessions)
                .filter(
                    ChargingSessions.id_charger == charger_id,
                    extract("hour", ChargingSessions.start_time) == hour,
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
    """
    ONLY TO USE WITH GENERIC CHARGER ID
    """

    latest = (
        db.query(EvForecastStats)
        .filter(
            EvForecastStats.id_charger == charger_id,
            EvForecastStats.hour == hour,
        )
        .order_by(EvForecastStats.updated_at.desc())
        .first()
    )

    if latest is None:
        # Create initial stat row
        stat = EvForecastStats(
            id=uuid4(),
            hour=hour,
            mean_energy_kwh=energy_kwh,
            std_energy_kwh=0.0,
            mean_duration_hours=duration_hours,
            std_duration_hours=0.0,
            sample_count=1,
            id_charger=charger_id,
        )
        db.add(stat)
        return

    # Otherwise derive a new aggregated stat and append it
    energy_var = (latest.std_energy_kwh ** 2) * latest.sample_count
    duration_var = (latest.std_duration_hours ** 2) * latest.sample_count

    mean_energy, energy_var, n = _update_online_stats(
        latest.mean_energy_kwh, energy_var, latest.sample_count, energy_kwh
    )
    mean_duration, duration_var, _ = _update_online_stats(
        latest.mean_duration_hours, duration_var, latest.sample_count, duration_hours
    )

    stat = EvForecastStats(
        id=uuid4(),
        hour=hour,
        mean_energy_kwh=mean_energy,
        std_energy_kwh=sqrt(energy_var / n),
        mean_duration_hours=mean_duration,
        std_duration_hours=sqrt(duration_var / n),
        sample_count=n,
        id_charger=charger_id,
    )
    db.add(stat)


def _initialize_new_stat(
    db: Session,
    charger_id: str,
    hour: int,
    sessions: list[ChargingSessions],
):
    energy_values = [s.energy_delivered_kwh for s in sessions]
    duration_values = [(s.end_charging_time - s.start_time).total_seconds() / 3600 for s in sessions]

    mean_energy = float(np.mean(energy_values))
    std_energy = float(np.std(energy_values, ddof=0))
    mean_duration = float(np.mean(duration_values))
    std_duration = float(np.std(duration_values, ddof=0))
    sample_count = len(sessions)

    stat = EvForecastStats(
        id=uuid4(),
        hour=hour,
        mean_energy_kwh=mean_energy,
        std_energy_kwh=std_energy,
        mean_duration_hours=mean_duration,
        std_duration_hours=std_duration,
        sample_count= sample_count,
        id_charger=charger_id,
    )
    db.add(stat)
