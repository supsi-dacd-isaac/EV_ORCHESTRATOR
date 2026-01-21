from typing import List
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models.db.ev_duration_cdf import EVDurationCDFDB
from app.services.common.constants import GENERIC_CHARGER_ID


def get_cumulative_duration_probability(
    charger_id: str,
    connected_time_hours: float,
) -> float:
    """
    Returns P(duration <= connected_time_hours) --> which could be interpreted as a cumulative probability of disconnection
    using linear interpolation over stored CDF horizons.
    """

    db: Session = SessionLocal()
    try:
        # 1. Try charger-specific CDF
        rows = (
            db.query(EVDurationCDFDB)
            .filter(
                EVDurationCDFDB.charger_id == charger_id,
                EVDurationCDFDB.hour == -1,
            )
            .order_by(EVDurationCDFDB.horizon_hours)
            .all()
        )

        # 2. Fallback to generic
        if not rows:
            rows = (
                db.query(EVDurationCDFDB)
                .filter(
                    EVDurationCDFDB.charger_id == GENERIC_CHARGER_ID,
                    EVDurationCDFDB.hour == -1,
                )
                .order_by(EVDurationCDFDB.horizon_hours)
                .all()
            )

        if not rows:
            return 0.0  # absolute fallback

        horizons = [r.horizon_hours for r in rows]
        probs = [r.probability for r in rows]

        # 3. Clamp
        if connected_time_hours <= horizons[0]:
            return probs[0]
        if connected_time_hours >= horizons[-1]:
            return probs[-1]

        # 4. Linear interpolation
        for i in range(len(horizons) - 1):
            h0, h1 = horizons[i], horizons[i + 1]
            if h0 <= connected_time_hours <= h1:
                p0, p1 = probs[i], probs[i + 1]
                alpha = (connected_time_hours - h0) / (h1 - h0)
                return p0 + alpha * (p1 - p0)

        return probs[-1]

    finally:
        db.close()
