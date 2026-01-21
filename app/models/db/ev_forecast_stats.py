from sqlalchemy import Column, String, Integer, Float, DateTime
from app.models.db.base import Base
from datetime import datetime

class EVForecastStatsDB(Base):
    __tablename__ = "ev_forecast_stats"

    charger_id = Column(String, primary_key=True)
    hour = Column(Integer, primary_key=True)

    mean_energy_kwh = Column(Float, nullable=False)
    std_energy_kwh = Column(Float, nullable=False)

    mean_duration_hours = Column(Float, nullable=False)
    std_duration_hours = Column(Float, nullable=False)

    sample_count = Column(Integer, nullable=False)

    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)
