from sqlalchemy import Column, Integer, String, Float, DateTime, UniqueConstraint
from datetime import datetime
from app.models.db.base import Base

class EVDurationCDFDB(Base):
    __tablename__ = "ev_duration_cdf"

    id = Column(Integer, primary_key=True, index=True)

    charger_id = Column(String, index=True, nullable=False)
    hour = Column(Integer, index=True, nullable=False)  # -1 = all hours
    horizon_hours = Column(Float, nullable=False)

    probability = Column(Float, nullable=False)
    sample_count = Column(Integer, nullable=False, default=0)

    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint(
            "charger_id", "hour", "horizon_hours",
            name="uq_duration_cdf_charger_hour_horizon"
        ),
    )
