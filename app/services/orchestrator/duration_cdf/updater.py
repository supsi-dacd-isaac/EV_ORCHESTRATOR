from datetime import datetime
from sqlalchemy.orm import Session
from sqlalchemy import extract
import uuid

from app.models import EvDurationCdf
from app.models import ChargingSessions

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
        ChargingSessions.start_time,
        ChargingSessions.end_time,
    )

    # Generic = all chargers
    if charger_id != GENERIC_CHARGER_ID:
        query = query.filter(ChargingSessions.id_charger == charger_id)

    # Hour-specific bucket
    if hour != -1:
        query = query.filter(extract("hour", ChargingSessions.start_time) == hour)

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
        EvDurationCdf(
            id=uuid.uuid4(),
            hour=hour,
            horizon_hours=horizon,
            probability=prob,
            sample_count=n,
            updated_at=datetime.utcnow(),
            id_charger=charger_id,
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
    Perform an online update of the CDF rows by appending new rows.
    Retrieves the latest rows (by updated_at) for the given (charger_id, hour),
    computes updated probabilities, and appends new rows with the new stats.
    """

    # Get latest batch of CDF rows for this (charger_id, hour)
    latest_rows = (
        db.query(EvDurationCdf)
        .filter(
            EvDurationCdf.id_charger == charger_id,
            EvDurationCdf.hour == hour,
        )
        .order_by(EvDurationCdf.updated_at.desc())
        .all()
    )

    if not latest_rows:
        return

    # All rows should have the same updated_at; filter to ensure consistency
    latest_updated = latest_rows[0].updated_at
    latest_rows = [r for r in latest_rows if r.updated_at == latest_updated]

    # Compute updated probabilities
    new_rows = []
    for row in latest_rows:
        indicator = 1.0 if duration_hours <= row.horizon_hours else 0.0
        n = row.sample_count
        new_prob = (row.probability * n + indicator) / (n + 1)
        new_sample_count = n + 1

        new_row = EvDurationCdf(
            id=uuid.uuid4(),
            hour=hour,
            horizon_hours=row.horizon_hours,
            probability=new_prob,
            sample_count=new_sample_count,
            updated_at=datetime.utcnow(),
            id_charger=charger_id,
        )
        new_rows.append(new_row)

    db.add_all(new_rows)


def update_ev_duration_cdf(
    charger_id: str,
    start_time: datetime,
    duration_hours: float,
):
    """
    Update the EV duration CDF after a session disconnects.

    - Always updates GENERIC (hour=-1 and hour=start_time.hour)
    - Updates charger-specific only if MIN_SESSIONS_FOR_CHARGER_SPECIFIC reached
    - Appends new rows instead of modifying existing ones
    """

    hour = start_time.hour

    with get_db_session() as db:

        # -----------------------------
        # GENERIC (always)
        # -----------------------------
        for h in (-1, hour):
            latest = (
                db.query(EvDurationCdf)
                .filter(
                    EvDurationCdf.id_charger == GENERIC_CHARGER_ID,
                    EvDurationCdf.hour == h,
                )
                .order_by(EvDurationCdf.updated_at.desc())
                .first()
            )

            if latest is None:
                _initialize_cdf_from_sessions(db, GENERIC_CHARGER_ID, h)
            else:
                _online_update_cdf(db, GENERIC_CHARGER_ID, h, duration_hours)

        # -----------------------------
        # CHARGER-SPECIFIC
        # -----------------------------
        for h in (-1, hour):

            latest = (
                db.query(EvDurationCdf)
                .filter(
                    EvDurationCdf.id_charger == charger_id,
                    EvDurationCdf.hour == h,
                )
                .order_by(EvDurationCdf.updated_at.desc())
                .first()
            )

            if latest is None:
                count = completed_sessions_count(db, charger_id, h)
                if count < MIN_SESSIONS_FOR_CHARGER_SPECIFIC:
                    continue
                _initialize_cdf_from_sessions(db, charger_id, h)

            else:
                _online_update_cdf(db, charger_id, h, duration_hours)

