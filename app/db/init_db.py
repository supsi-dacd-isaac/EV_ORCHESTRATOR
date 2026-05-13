import uuid

from app.config import ADMIN_INIT_COMPANY, ADMIN_INIT_PASSWORD, ADMIN_INIT_USER
from app.db.session import SessionLocal, engine
from app.models import Base, Chargers, ChargingSessions, EvForecastStats, EvDurationCdf, Owners, Actions, GridLoadForecasted, EvPilotForecastTimeseries
from app.services.common.auth import hash_password
from app.services.common.constants import GENERIC_CHARGER_ID


def init_db():
    Base.metadata.create_all(bind=engine)
    _seed_admin()


def _seed_admin():
    """Create the initial admin owner and generic charger if the owners table is empty."""
    db = SessionLocal()
    try:
        if db.query(Owners).first() is not None:
            return  # table already has data, skip seeding

        if not ADMIN_INIT_PASSWORD:
            print(
                "WARNING: ADMIN_INIT_PASSWORD is not set. "
                "Skipping admin seed. Set it in .env or as an environment variable."
            )
            return

        admin = Owners(
            id=uuid.uuid4(),
            user=ADMIN_INIT_USER,
            password=hash_password(ADMIN_INIT_PASSWORD),
            company_name=ADMIN_INIT_COMPANY,
            type="global",
            role="admin",
        )
        db.add(admin)
        db.flush()  # make admin.id available for the charger FK

        generic_charger = Chargers(
            id=uuid.UUID(GENERIC_CHARGER_ID),
            name="_generic",
            type="V1G",
            latitude=0,
            longitude=0,
            nominal_power=100,
            plugs="1",
            id_owner=admin.id,
            id_pilot=None,
        )
        db.add(generic_charger)
        db.commit()
        print(f"Created initial admin user '{ADMIN_INIT_USER}' and generic charger.")
    finally:
        db.close()


