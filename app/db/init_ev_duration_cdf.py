from datetime import datetime
import uuid
from app.db.session import SessionLocal
from app.models import EvDurationCdf
from app.db.ev_duration_cdf_defaults import DEFAULT_DURATION_CDF


def init_ev_duration_cdf(charger_id: str):
    db = SessionLocal()
    try:
        for horizon, (prob, sample_count) in DEFAULT_DURATION_CDF.items():
            db.add(
                EvDurationCdf(
                    id=uuid.uuid4(),
                    id_charger=charger_id,
                    hour=-1,
                    horizon_hours=horizon,
                    probability=prob,
                    sample_count=sample_count,
                    updated_at=datetime.utcnow(),
                )
            )
        db.commit()
        print("Initialized EV duration CDF table")
    except Exception as e:
        db.rollback()
        raise
    finally:
        db.close()