from datetime import datetime
from math import sqrt
from typing import List, Optional
from sqlalchemy.orm import Session
import numpy as np
from uuid import uuid4

from app.models import EvForecastStats
from app.services.common.db_utils import (
    get_db_session,
    completed_sessions_count,
    get_pilot_tz_for_charger,
    get_local_hour,
)
from app.services.common.constants import GENERIC_CHARGER_ID, MIN_SESSIONS_FOR_CHARGER_SPECIFIC
from app.models import ChargingSessions

# Outliers excluded only from the shared generic fleet model (not charger-specific).
GENERIC_MAX_ENERGY_KWH = 80.0
GENERIC_MAX_DURATION_HOURS = 72.0  # 3 days


def _update_online_stats(mean: float, var: float, n: int, x: float):
    n_new = n + 1
    delta = x - mean
    mean_new = mean + delta / n_new
    delta2 = x - mean_new
    var_new = var + delta * delta2
    return mean_new, var_new, n_new


def _within_generic_limits(energy_kwh: float, duration_hours: float) -> bool:
    """True if energy/duration are allowed into the generic forecast stats."""
    return (
        energy_kwh is not None
        and duration_hours is not None
        and energy_kwh <= GENERIC_MAX_ENERGY_KWH
        and duration_hours <= GENERIC_MAX_DURATION_HOURS
        and energy_kwh >= 0
        and duration_hours >= 0
    )


def _session_duration_hours(session: ChargingSessions) -> Optional[float]:
    """Charging duration for stats: prefer end_charging_time, else end_time."""
    if session.start_time is None:
        return None
    end = session.end_charging_time or session.end_time
    if end is None:
        return None
    return (end - session.start_time).total_seconds() / 3600


def _sessions_for_local_hour(
    db: Session,
    *,
    charger_id: str,
    local_hour: int,
    tz_name: str,
) -> List[ChargingSessions]:
    """Completed sessions whose pilot-local start hour equals ``local_hour``.

    For the generic charger, each session uses its own charger's pilot timezone,
    and energy/duration outliers above the generic caps are excluded.
    """
    if charger_id == GENERIC_CHARGER_ID:
        all_sessions = (
            db.query(ChargingSessions)
            .filter(ChargingSessions.end_time.isnot(None))
            .all()
        )
        sessions = []
        for s in all_sessions:
            if s.start_time is None or s.id_charger is None:
                continue
            session_tz = get_pilot_tz_for_charger(db, str(s.id_charger))
            if get_local_hour(s.start_time, session_tz) != local_hour:
                continue
            dur = _session_duration_hours(s)
            if dur is None or not _within_generic_limits(s.energy_delivered_kwh, dur):
                continue
            sessions.append(s)
        return sessions

    all_sessions = (
        db.query(ChargingSessions)
        .filter(
            ChargingSessions.id_charger == charger_id,
            ChargingSessions.end_time.isnot(None),
        )
        .all()
    )
    return [
        s
        for s in all_sessions
        if s.start_time is not None and get_local_hour(s.start_time, tz_name) == local_hour
    ]


def update_ev_forecast(
    db: Session,
    charger_id: str,
    start_time: datetime,
    energy_kwh: float,
    duration_hours: float,
):
    """
    Update EV forecast statistics after a completed charging session.
    Does NOT commit - caller is responsible for commit.
    """

    # Resolve the pilot's local timezone and compute the local hour for this session.
    # The same local_hour is used for both the charger-specific and generic charger
    # records so that the generic charger statistics are indexed in the same space as
    # the real charger's statistics (i.e. pilot-local time, not UTC).
    tz_name = get_pilot_tz_for_charger(db, charger_id)
    local_hour = get_local_hour(start_time, tz_name)

    # -----------------------------------
    # Always update GENERIC forecast (append new row)
    # -----------------------------------
    _update_or_initialize_stat(
        db,
        charger_id=GENERIC_CHARGER_ID,
        local_hour=local_hour,
        energy_kwh=energy_kwh,
        duration_hours=duration_hours,
        tz_name=tz_name,
    )

    # -----------------------------------
    # Charger-specific logic
    # -----------------------------------
    charger_stat = (
        db.query(EvForecastStats).filter(
            EvForecastStats.id_charger == charger_id,
            EvForecastStats.local_hour == local_hour,
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
            local_hour=local_hour,
            mean_energy_kwh=mean_energy,
            std_energy_kwh=sqrt(energy_var / n),
            mean_duration_hours=mean_duration,
            std_duration_hours=sqrt(duration_var / n),
            sample_count=n,
            id_charger=charger_id,
        )
        db.add(new_stat)

    else:
        sessions = _sessions_for_local_hour(
            db, charger_id=str(charger_id), local_hour=local_hour, tz_name=tz_name
        )

        if len(sessions) >= MIN_SESSIONS_FOR_CHARGER_SPECIFIC:
            _initialize_new_stat(
                db,
                charger_id,
                local_hour,
                sessions,
            )


def _update_or_initialize_stat(
    db: Session,
    charger_id: str,
    local_hour: int,
    energy_kwh: float,
    duration_hours: float,
    tz_name: str = "UTC",
):
    """
    ONLY TO USE WITH GENERIC CHARGER ID

    When no stats row exists for this hour, rebuild from all completed sessions
    in that pilot-local hour (so deleting stats + fixing sessions can recalculate).
    The current session is already committed before this runs, so it is included
    only if it passes the generic energy/duration caps.
    """
    current_ok = _within_generic_limits(energy_kwh, duration_hours)

    latest = (
        db.query(EvForecastStats)
        .filter(
            EvForecastStats.id_charger == charger_id,
            EvForecastStats.local_hour == local_hour,
        )
        .order_by(EvForecastStats.updated_at.desc())
        .first()
    )

    if latest is None:
        sessions = _sessions_for_local_hour(
            db, charger_id=charger_id, local_hour=local_hour, tz_name=tz_name
        )
        if sessions:
            _initialize_new_stat(db, charger_id, local_hour, sessions)
            return

        # No usable historical sessions for this hour: fall back to this session
        # alone only if it is within generic caps.
        if not current_ok:
            return
        stat = EvForecastStats(
            id=uuid4(),
            local_hour=local_hour,
            mean_energy_kwh=energy_kwh,
            std_energy_kwh=0.0,
            mean_duration_hours=duration_hours,
            std_duration_hours=0.0,
            sample_count=1,
            id_charger=charger_id,
        )
        db.add(stat)
        return

    # Online update: skip outliers so they never enter the generic model.
    if not current_ok:
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
        local_hour=local_hour,
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
    local_hour: int,
    sessions: list[ChargingSessions],
):
    energy_values = []
    duration_values = []
    for s in sessions:
        dur = _session_duration_hours(s)
        if dur is None:
            continue
        energy_values.append(s.energy_delivered_kwh)
        duration_values.append(dur)

    if not energy_values:
        return

    mean_energy = float(np.mean(energy_values))
    std_energy = float(np.std(energy_values, ddof=0))
    mean_duration = float(np.mean(duration_values))
    std_duration = float(np.std(duration_values, ddof=0))
    sample_count = len(energy_values)

    stat = EvForecastStats(
        id=uuid4(),
        local_hour=local_hour,
        mean_energy_kwh=mean_energy,
        std_energy_kwh=std_energy,
        mean_duration_hours=mean_duration,
        std_duration_hours=std_duration,
        sample_count=sample_count,
        id_charger=charger_id,
    )
    db.add(stat)
