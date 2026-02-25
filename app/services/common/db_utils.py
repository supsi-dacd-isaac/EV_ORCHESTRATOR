# python
from contextlib import contextmanager
from typing import Generator

from sqlalchemy.orm import Session
from sqlalchemy import extract

from app.db.session import SessionLocal
from app.models import ChargingSessions


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
    hour: int,
) -> int:
    """
    Count completed charging sessions for a charger.
    If hour == -1, count sessions across all hours.
    """

    query = db.query(ChargingSessions).filter(
        ChargingSessions.id_charger == charger_id
    )

    if hour != -1:
        query = query.filter(
            extract("hour", ChargingSessions.start_time) == hour
        )

    return query.count()
