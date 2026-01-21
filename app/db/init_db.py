from app.db.session import engine
from app.models.db.base import Base
from app.models.db.charging_session import ChargingSessionDB
from app.models.db.ev_forecast_stats import EVForecastStatsDB
from app.models.db.ev_duration_cdf import EVDurationCDFDB

def init_db():
    Base.metadata.create_all(bind=engine)