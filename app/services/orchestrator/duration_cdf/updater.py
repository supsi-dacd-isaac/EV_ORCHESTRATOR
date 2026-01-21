from datetime import datetime
from sqlalchemy.orm import Session
from sqlalchemy import extract

from app.models.db.ev_duration_cdf import EVDurationCDFDB
from app.models.db.charging_session import ChargingSessionDB

from app.db.ev_duration_cdf_defaults import DEFAULT_DURATION_CDF
from app.services.common.constants import (
    GENERIC_CHARGER_ID,
    MIN_SESSIONS_FOR_CHARGER_SPECIFIC,
)
from app.services.common.db_utils import (
    get_db_session,
    completed_sessions_count,
)


def _compute_cdf_from_durations(durations: list[float]) -> dict[float, tuple[float, int]]:
    """
    Build an empirical CDF from a list of durations.

    Returns:
        dict[horizon_hours] = (probability, sample_count)
    """
    n = len(durations)
    if n == 0:
        return {}

    result = {}
    for horizon in DEFAULT_DURATION_CDF.keys():
        count_leq = sum(1 for d in durations if d <= horizon)
        probability = count_leq / n
        result[horizon] = (probability, n)

    return result

def _initialize_cdf_from_sessions(
    db: Session,
    charger_id: str,
    hour: int,
):
    """
    Initialize the CDF table for (charger_id, hour) using historical sessions.
    """

    query = db.query(
        ChargingSessionDB.start_time,
        ChargingSessionDB.end_time,
    )

    # Generic = all chargers
    if charger_id != GENERIC_CHARGER_ID:
        query = query.filter(ChargingSessionDB.charger_id == charger_id)

    # Hour-specific bucket
    if hour != -1:
        query = query.filter(extract("hour", ChargingSessionDB.start_time) == hour)

    sessions = query.all()

    durations = [
        (row.end_time - row.start_time).total_seconds() / 3600
        for row in sessions
        if row.end_time is not None
    ]

    if not durations:
        print('No completed sessions found to initialize CDF for charger_id=', charger_id, ' hour=', hour)
        return

    cdf = _compute_cdf_from_durations(durations)

    rows = [
        EVDurationCDFDB(
            charger_id=charger_id,
            hour=hour,
            horizon_hours=horizon,
            probability=prob,
            sample_count=n,
        )
        for horizon, (prob, n) in cdf.items()
    ]

    db.add_all(rows)

def _online_update_cdf(
    db: Session,
    charger_id: str,
    hour: int,
    duration_hours: float,
):
    """
    Perform an online update of the CDF rows.
    Assumes rows already exist.
    """

    rows = (
        db.query(EVDurationCDFDB)
        .filter(
            EVDurationCDFDB.charger_id == charger_id,
            EVDurationCDFDB.hour == hour,
        )
        .all()
    )

    for row in rows:
        indicator = 1.0 if duration_hours <= row.horizon_hours else 0.0
        n = row.sample_count
        row.probability = (row.probability * n + indicator) / (n + 1)
        row.sample_count = n + 1


def update_ev_duration_cdf(
    charger_id: str,
    start_time: datetime,
    duration_hours: float,
):
    """
    Update the EV duration CDF after a session disconnects.

    - Always updates GENERIC (-1 and hour)
    - Updates charger-specific only if MIN_SESSIONS_FOR_CHARGER_SPECIFIC reached
    """

    hour = start_time.hour

    with get_db_session() as db:

        # -----------------------------
        # GENERIC (always)
        # -----------------------------
        for h in (-1, hour):
            exists = (
                db.query(EVDurationCDFDB)
                .filter(
                    EVDurationCDFDB.charger_id == GENERIC_CHARGER_ID,
                    EVDurationCDFDB.hour == h,
                )
                .first()
            )

            if exists is None:
                _initialize_cdf_from_sessions(db, GENERIC_CHARGER_ID, h)
            else:
                _online_update_cdf(db, GENERIC_CHARGER_ID, h, duration_hours)

        # -----------------------------
        # CHARGER-SPECIFIC
        # -----------------------------
        for h in (-1, hour):

            exists = (
                db.query(EVDurationCDFDB)
                .filter(
                    EVDurationCDFDB.charger_id == charger_id,
                    EVDurationCDFDB.hour == h,
                )
                .first()
            )

            if exists is None:
                count = completed_sessions_count(db, charger_id, h)
                if count < MIN_SESSIONS_FOR_CHARGER_SPECIFIC:
                    continue
                _initialize_cdf_from_sessions(db, charger_id, h)

            else:
                _online_update_cdf(db, charger_id, h, duration_hours)

