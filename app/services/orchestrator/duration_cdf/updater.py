from datetime import datetime
from sqlalchemy.orm import Session
from sqlalchemy import text
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
    get_pilot_tz_for_charger,
    get_local_hour,
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
    local_hour: int,
    tz_name: str = "UTC",
):
    """
    Initialize the CDF table for (charger_id, local_hour) using historical sessions.
    local_hour=-1 means all hours (global CDF for this charger).
    """

    if charger_id != GENERIC_CHARGER_ID:
        all_sessions = (
            db.query(ChargingSessions)
            .filter(
                ChargingSessions.id_charger == charger_id,
                ChargingSessions.end_time.isnot(None),
            )
            .all()
        )
    else:
        all_sessions = (
            db.query(ChargingSessions)
            .filter(ChargingSessions.end_time.isnot(None))
            .all()
        )

    # Filter by local hour in Python (avoids SQL-level UTC hour extraction).
    # For the generic charger each session belongs to a different real charger
    # and therefore potentially a different pilot timezone.  We must resolve tz
    # per-session rather than applying a single global tz.
    if local_hour != -1:
        if charger_id == GENERIC_CHARGER_ID:
            filtered = []
            for s in all_sessions:
                if s.start_time is not None:
                    session_tz = get_pilot_tz_for_charger(db, str(s.id_charger))
                    if get_local_hour(s.start_time, session_tz) == local_hour:
                        filtered.append(s)
            sessions = filtered
        else:
            sessions = [
                s for s in all_sessions
                if s.start_time is not None and get_local_hour(s.start_time, tz_name) == local_hour
            ]
    else:
        sessions = all_sessions

    durations = [
        (s.end_time - s.start_time).total_seconds() / 3600
        for s in sessions
        if s.end_time is not None
    ]

    if not durations:
        return

    cdf = _compute_cdf_from_durations(durations)

    rows = [
        EvDurationCdf(
            id=uuid.uuid4(),
            local_hour=local_hour,
            horizon_hours=horizon,
            probability=prob,
            sample_count=n,
            id_charger=charger_id,
        )
        for horizon, (prob, n) in cdf.items()
    ]

    db.add_all(rows)


def _online_update_cdf(
    db: Session,
    charger_id: str,
    local_hour: int,
    duration_hours: float,
):
    """
    Perform an online update of the CDF rows by appending new rows.
    Retrieves the latest rows (by sample_count) for the given (charger_id, local_hour),
    computes updated probabilities, and appends new rows with the new stats.
    Uses advisory lock to prevent concurrent update races.
    Does NOT commit - caller is responsible for commit.
    """

    # Acquire advisory lock keyed by (charger_id hash, local_hour) to prevent concurrent updates
    lock_id = hash((str(charger_id), local_hour)) & 0x7FFFFFFF  # Keep positive for Postgres
    db.execute(text(f"SELECT pg_advisory_lock({lock_id})"))

    try:
        # Get latest batch of CDF rows for this (charger_id, hour)
        # Order by sample_count desc to get the most recent version
        latest_rows = (
            db.query(EvDurationCdf)
            .filter(
                EvDurationCdf.id_charger == charger_id,
                EvDurationCdf.local_hour == local_hour,
            )
            .order_by(EvDurationCdf.sample_count.desc(), EvDurationCdf.updated_at.desc())
            .all()
        )

        if not latest_rows:
            return

        # All rows in the latest batch should have the same sample_count (version)
        latest_sample_count = latest_rows[0].sample_count
        latest_rows = [r for r in latest_rows if r.sample_count == latest_sample_count]

        # Compute updated probabilities
        new_rows = []
        for row in latest_rows:
            indicator = 1.0 if duration_hours <= row.horizon_hours else 0.0
            n = row.sample_count
            new_prob = (row.probability * n + indicator) / (n + 1)
            new_sample_count = n + 1

            new_row = EvDurationCdf(
                id=uuid.uuid4(),
                local_hour=local_hour,
                horizon_hours=row.horizon_hours,
                probability=new_prob,
                sample_count=new_sample_count,
                id_charger=charger_id,
            )
            new_rows.append(new_row)

        db.add_all(new_rows)
    finally:
        # Release advisory lock
        db.execute(text(f"SELECT pg_advisory_unlock({lock_id})"))


def update_ev_duration_cdf(
    db: Session,
    charger_id: str,
    start_time: datetime,
    duration_hours: float,
):
    """
    Update the EV duration CDF after a session disconnects.

    - Always updates GENERIC (local_hour=-1 and local_hour=pilot-local start hour)
    - Updates charger-specific only if MIN_SESSIONS_FOR_CHARGER_SPECIFIC reached
    - Appends new rows instead of modifying existing ones
    - Does NOT commit - caller is responsible for commit.
    """

    # Resolve the pilot's local timezone and compute the local hour for this session.
    # The generic charger uses the REAL charger's pilot timezone so that both
    # generic and charger-specific statistics are indexed in the same local-time space.
    tz_name = get_pilot_tz_for_charger(db, charger_id)
    local_hour = get_local_hour(start_time, tz_name)

    # -----------------------------
    # GENERIC (always) — hour=-1 (global) and hour=local_hour
    # -----------------------------
    for h in (-1, local_hour):
        latest = (
            db.query(EvDurationCdf)
            .filter(
                EvDurationCdf.id_charger == GENERIC_CHARGER_ID,
                EvDurationCdf.local_hour == h,
            )
            .order_by(EvDurationCdf.sample_count.desc(), EvDurationCdf.updated_at.desc())
            .first()
        )

        if latest is None:
            _initialize_cdf_from_sessions(db, GENERIC_CHARGER_ID, h, tz_name)
        else:
            _online_update_cdf(db, GENERIC_CHARGER_ID, h, duration_hours)

    # -----------------------------
    # CHARGER-SPECIFIC
    # -----------------------------
    for h in (-1, local_hour):
        latest = (
            db.query(EvDurationCdf)
            .filter(
                EvDurationCdf.id_charger == charger_id,
                EvDurationCdf.local_hour == h,
            )
            .order_by(EvDurationCdf.sample_count.desc(), EvDurationCdf.updated_at.desc())
            .first()
        )

        if latest is None:
            count = completed_sessions_count(db, charger_id, h, tz_name)
            if count < MIN_SESSIONS_FOR_CHARGER_SPECIFIC:
                continue
            _initialize_cdf_from_sessions(db, charger_id, h, tz_name)

        else:
            _online_update_cdf(db, charger_id, h, duration_hours)

