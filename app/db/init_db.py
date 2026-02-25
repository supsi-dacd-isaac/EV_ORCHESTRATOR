from app.db.session import engine
from app.models import Base, Chargers, ChargingSessions, EvForecastStats, EvDurationCdf, Owners


def init_db():
    Base.metadata.create_all(bind=engine)