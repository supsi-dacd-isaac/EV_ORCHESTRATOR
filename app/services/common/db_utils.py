# python
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Generator, Union
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models import ChargingSessions


def to_utc(value: datetime) -> datetime:
    """
    Normalise *value* to a UTC-aware datetime for storing in a
    ``TIMESTAMP WITH TIME ZONE`` column.

    Convention used throughout this repo:
    - DB columns  : TIMESTAMP WITH TIME ZONE → stores UTC, returns UTC-aware.
    - API inputs  : FastAPI/Pydantic accepts any tz-aware ISO-8601 string
                    (e.g. "2026-05-11T15:00:00+02:00") or a naive string
                    (treated as UTC). Always call to_utc() before writing.
    - API outputs : UTC-aware ISO-8601 with "+00:00" suffix, e.g.
                    "2026-05-11T13:00:00+00:00". Clients must interpret as UTC.
    - Internal    : datetime objects with tzinfo=timezone.utc.

    Examples
    --------
    to_utc(datetime(2026,5,11,15,0, tzinfo=ZoneInfo("Europe/Zurich")))
        → datetime(2026,5,11,13,0, tzinfo=timezone.utc)   # +02:00 → UTC
    to_utc(datetime(2026,5,11,13,0))
        → datetime(2026,5,11,13,0, tzinfo=timezone.utc)   # naive → assumed UTC
    """
    if value is None:
        return value
    if value.tzinfo is None:
        # Naive input: assume it is already UTC wall time
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def to_response_tz(
    value: datetime,
    tz: Union[str, ZoneInfo, timezone, None],
) -> datetime:
    """
    Convert a UTC-aware datetime to the caller's timezone for API responses.

    Parameters
    ----------
    value : datetime
        UTC-aware datetime (as stored/returned by the DB).
    tz : str | ZoneInfo | timezone | None
        Target timezone.  Can be:
        - A string IANA name: "Europe/Zurich"
        - A tzinfo from an incoming request timestamp (e.g. timezone(timedelta(hours=2)))
        - A ZoneInfo instance
        - None / "UTC" → return unchanged (UTC)

    Examples
    --------
    to_response_tz(datetime(2026,5,12,10,0, tzinfo=timezone.utc), "Europe/Zurich")
        → datetime(2026,5,12,12,0, tzinfo=ZoneInfo("Europe/Zurich"))  # CEST +02:00
    """
    if value is None:
        return value
    if tz is None:
        return value
    if isinstance(tz, str):
        if tz.upper() == "UTC":
            return value
        try:
            tz = ZoneInfo(tz)
        except (ZoneInfoNotFoundError, KeyError):
            return value  # unknown name → return as UTC
    return value.astimezone(tz)


def get_pilot_tz_for_charger(db: Session, charger_id: str) -> str:
    """
    Resolve charger → pilot → timezone_name.
    Returns 'Europe/Zurich' as default when the charger has no pilot or
    the pilot has no timezone set.
    """
    from app.models import Chargers, Pilot  # local import to avoid circular deps
    charger = db.query(Chargers).filter(Chargers.id == charger_id).first()
    if charger is None or charger.id_pilot is None:
        return "Europe/Zurich"
    pilot = db.query(Pilot).filter(Pilot.id == charger.id_pilot).first()
    if pilot is None:
        return "Europe/Zurich"
    return pilot.timezone_name or "Europe/Zurich"


def to_pilot_time(dt_utc: datetime, tz_name: str) -> datetime:
    """Convert a UTC-aware datetime to pilot-local time."""
    return to_response_tz(to_utc(dt_utc), tz_name)


def get_local_hour(dt_utc: datetime, tz_name: str) -> int:
    """Convert UTC datetime to pilot-local time and return the local hour (0-23)."""
    return to_pilot_time(dt_utc, tz_name).hour


@contextmanager
def get_db_session() -> Generator[Session, None, None]:
    """
    Context-managed SQLAlchemy session.

    Guarantees:
    - commit on success
    - rollback on exception
    - always closes the session
    """
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

def completed_sessions_count(
    db: Session,
    charger_id: str,
    local_hour: int,
    tz_name: str = "UTC",
) -> int:
    """
    Count COMPLETED charging sessions for a charger (end_time is not null),
    filtered by pilot-local hour.
    If local_hour == -1, count sessions across all hours.
    """
    query = db.query(ChargingSessions).filter(
        ChargingSessions.id_charger == charger_id,
        ChargingSessions.end_time.isnot(None),
    )

    if local_hour == -1:
        return query.count()

    all_sessions = query.all()
    return sum(
        1 for s in all_sessions
        if s.start_time is not None and get_local_hour(s.start_time, tz_name) == local_hour
    )
