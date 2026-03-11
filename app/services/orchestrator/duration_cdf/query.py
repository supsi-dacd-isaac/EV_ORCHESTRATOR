from typing import List
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models import EvDurationCdf
from app.services.common.constants import GENERIC_CHARGER_ID


def get_cumulative_duration_probability(
    charger_id: str,
    connected_time_hours: float,
) -> float:
    """
    Returns P(duration <= connected_time_hours) --> which could be interpreted as a cumulative probability of disconnection - not exactly the same because it should be conditional
    using linear interpolation over stored CDF horizons.

    Uses the latest stored EvDurationCdf rows (by sample_count) for the given charger/hour=-1.
    Falls back to GENERIC_CHARGER_ID if charger-specific CDFs are not present.
    """

    db: Session = SessionLocal()
    try:
        # 1. Try latest charger-specific CDF rows (order by sample_count desc, then by horizon_hours)
        rows = (
            db.query(EvDurationCdf)
            .filter(
                EvDurationCdf.id_charger == charger_id,
                EvDurationCdf.hour == -1, #TODO: consider hour-specific CDFs in the future, when not available fallback to hour=-1
            )
            .order_by(EvDurationCdf.sample_count.desc(), EvDurationCdf.updated_at.desc(), EvDurationCdf.horizon_hours)
            .all()
        )

        # Filter to the latest batch (group by sample_count, take the highest version)
        if rows:
            latest_sample_count = rows[0].sample_count
            rows = [r for r in rows if r.sample_count == latest_sample_count]

        # 2. Fallback to generic
        if not rows:
            rows = (
                db.query(EvDurationCdf)
                .filter(
                    EvDurationCdf.id_charger == GENERIC_CHARGER_ID,
                    EvDurationCdf.hour == -1,
                )
                .order_by(EvDurationCdf.sample_count.desc(), EvDurationCdf.updated_at.desc(), EvDurationCdf.horizon_hours)
                .all()
            )

            if rows:
                latest_sample_count = rows[0].sample_count
                rows = [r for r in rows if r.sample_count == latest_sample_count]

        if not rows:
            return 0.5  # absolute fallback

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
