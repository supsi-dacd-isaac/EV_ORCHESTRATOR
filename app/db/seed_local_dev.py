"""
Create tables (SQLAlchemy) and seed a minimal dev tenant + charging history for local testing.

Matches ``id_pilot`` in ``ev_total_forecast_jobs.yaml`` example (00000000-0000-0000-0000-000000000001).

Usage::
    export DATABASE_URL=postgresql://postgres:postgre@localhost:5432/ev_orchestrator
    python -m app.db.seed_local_dev
    python -m app.db.seed_local_dev --force   # drop seed rows and re-insert

Requires: Postgres reachable at DATABASE_URL, dependencies from requirements.txt.
"""

from __future__ import annotations

import argparse
import random
import uuid
from datetime import datetime, timedelta

from app.db.session import SessionLocal, engine
from app.models import (
    Base,
    Chargers,
    ChargingSessions,
    EvDurationCdf,
    EvForecastStats,
    EvPilotForecastTimeseries,
    GridLoadForecasted,
    Owners,
    Pilot,
    Actions,
)

# Keep in sync with app/services/ev_forecast/ev_total_forecast_jobs.yaml example job.
SEED_OWNER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000e0")
SEED_PILOT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
SEED_CHARGER_IDS = (
    uuid.UUID("00000000-0000-0000-0000-0000000000c1"),
    uuid.UUID("00000000-0000-0000-0000-0000000000c2"),
)


def _wipe_seed_entities(db) -> None:
    """Remove rows tied to SEED_PILOT_ID / SEED_CHARGER_IDS / SEED_OWNER_ID."""
    cids = list(SEED_CHARGER_IDS)
    sids = [
        r[0]
        for r in db.query(ChargingSessions.id).filter(ChargingSessions.id_charger.in_(cids)).all()
    ]
    if sids:
        aids = [r[0] for r in db.query(Actions.id).filter(Actions.id_cs.in_(sids)).all()]
        if aids:
            db.query(GridLoadForecasted).filter(GridLoadForecasted.id_action.in_(aids)).delete(
                synchronize_session=False
            )
        db.query(Actions).filter(Actions.id_cs.in_(sids)).delete(synchronize_session=False)
        db.query(ChargingSessions).filter(ChargingSessions.id.in_(sids)).delete(
            synchronize_session=False
        )

    db.query(EvForecastStats).filter(EvForecastStats.id_charger.in_(cids)).delete(
        synchronize_session=False
    )
    db.query(EvDurationCdf).filter(EvDurationCdf.id_charger.in_(cids)).delete(
        synchronize_session=False
    )
    db.query(EvPilotForecastTimeseries).filter(
        EvPilotForecastTimeseries.id_pilot == SEED_PILOT_ID
    ).delete(synchronize_session=False)
    db.query(Chargers).filter(Chargers.id.in_(cids)).delete(synchronize_session=False)
    db.query(Pilot).filter(Pilot.id == SEED_PILOT_ID).delete(synchronize_session=False)
    db.query(Owners).filter(Owners.id == SEED_OWNER_ID).delete(synchronize_session=False)
    db.commit()


def _already_seeded(db) -> bool:
    return db.query(Pilot).filter(Pilot.id == SEED_PILOT_ID).first() is not None


def _generate_sessions(rng: random.Random, base_start: datetime, num_days: int) -> list[dict]:
    """Synthetic completed sessions across two chargers, spread over num_days."""
    rows: list[dict] = []
    for day in range(num_days):
        n = rng.randint(6, 18)
        for _ in range(n):
            hour = rng.randint(0, 23)
            minute = rng.randint(0, 59)
            start = base_start + timedelta(days=day, hours=hour, minutes=minute)
            dur_h = max(0.25, rng.uniform(0.4, 5.0))
            end = start + timedelta(hours=dur_h)
            energy = max(1.0, min(80.0, dur_h * rng.uniform(2.5, 12.0)))
            cid = rng.choice(SEED_CHARGER_IDS)
            rows.append(
                {
                    "start": start,
                    "end": end,
                    "duration_h": dur_h,
                    "energy": energy,
                    "charger_id": cid,
                }
            )
    rows.sort(key=lambda x: x["start"])
    return rows


def seed(db, *, force: bool) -> None:
    if _already_seeded(db):
        if not force:
            print(
                "Dev data already present (pilot 00000000-0000-0000-0000-000000000001). "
                "Use --force to remove and re-seed."
            )
            return
        print("Removing previous seed data (--force)...")
        _wipe_seed_entities(db)

    owner = Owners(
        id=SEED_OWNER_ID,
        user="dev_local_owner",
        password="not-used",
        company_name="Local Dev",
    )
    pilot = Pilot(id=SEED_PILOT_ID, name="local_dev_pilot", id_owner=SEED_OWNER_ID)
    db.add(owner)
    db.add(pilot)

    chargers = [
        Chargers(
            id=SEED_CHARGER_IDS[0],
            name="dev_charger_alpha",
            type="ac",
            latitude=47.37,
            longitude=8.54,
            nominal_power=11.0,
            plugs="1",
            id_owner=SEED_OWNER_ID,
            id_pilot=SEED_PILOT_ID,
        ),
        Chargers(
            id=SEED_CHARGER_IDS[1],
            name="dev_charger_beta",
            type="ac",
            latitude=47.38,
            longitude=8.55,
            nominal_power=22.0,
            plugs="2",
            id_owner=SEED_OWNER_ID,
            id_pilot=SEED_PILOT_ID,
        ),
    ]
    for c in chargers:
        db.add(c)

    rng = random.Random(42)
    base = datetime(2024, 6, 1, 0, 0, 0)
    spec = _generate_sessions(rng, base, num_days=120)

    for s in spec:
        sid = uuid.uuid4()
        db.add(
            ChargingSessions(
                id=sid,
                start_time=s["start"],
                end_time=s["end"],
                duration=s["duration_h"],
                energy_delivered_kwh=s["energy"],
                forecasted_energy_kwh=s["energy"],
                forecasted_energy_kwh_std=1.0,
                forecasted_duration_hours=s["duration_h"],
                forecasted_duration_hours_std=0.25,
                controlled_charging_points=1,
                active=True,
                id_charger=s["charger_id"],
            )
        )

    db.commit()
    print(f"Seeded owner, pilot, 2 chargers, {len(spec)} charging_sessions.")
    print(f"Use id_pilot = {SEED_PILOT_ID} in ev_total_forecast_jobs.yaml (example job already does).")


def main() -> None:
    parser = argparse.ArgumentParser(description="Create tables and seed local dev DB data.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Delete seed UUID rows and re-insert (only touches fixed dev IDs).",
    )
    parser.add_argument(
        "--tables-only",
        action="store_true",
        help="Only run SQLAlchemy create_all; do not insert seed data.",
    )
    args = parser.parse_args()

    print(f"Using database URL host: {engine.url.host!s} / db: {engine.url.database!s}")
    Base.metadata.create_all(bind=engine)
    print("Tables ensured (create_all).")

    if args.tables_only:
        return

    db = SessionLocal()
    try:
        seed(db, force=args.force)
    finally:
        db.close()


if __name__ == "__main__":
    main()
