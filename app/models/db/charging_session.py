from sqlalchemy import Column, String, Float, DateTime
from app.models.db.base import Base
import uuid


class ChargingSessionDB(Base):
    __tablename__ = "charging_sessions"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))

    charger_id = Column(String, nullable=False)
    start_time = Column(DateTime, nullable=False)
    end_time = Column(DateTime, nullable=False)
    end_charging_time = Column(DateTime, nullable=False)

    energy_delivered_kwh = Column(Float, nullable=False)

    forecasted_energy_kwh = Column(Float, nullable=False)
    forecasted_duration_hours = Column(Float, nullable=False)
