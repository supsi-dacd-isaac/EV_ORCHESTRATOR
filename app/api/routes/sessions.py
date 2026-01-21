from fastapi import APIRouter, HTTPException
from app.db.session import SessionLocal
from app.models.db.charging_session import ChargingSessionDB

router = APIRouter(prefix="/sessions", tags=["sessions"])

@router.get("/all_sessions")
def get_all_sessions():
    """
    Return all charging sessions stored in the DB
    """
    db = SessionLocal()
    try:
        sessions = db.query(ChargingSessionDB).all()

        return [
            {
                "id": s.id,
                "charger_id": s.charger_id,
                "start_time": s.start_time,
                "end_time": s.end_time,
                "energy_delivered_kwh": s.energy_delivered_kwh,
                "forecasted_energy_kwh": s.forecasted_energy_kwh,
                "forecasted_duration_hours": s.forecasted_duration_hours,
            }
            for s in sessions
        ]
    finally:
        db.close()

@router.get("/all_sessions/{charger_id}")
def get_charger_sessions(charger_id: str):
    """
    Return all sessions of an specific charger stored in the DB
    """
    db = SessionLocal()
    try:
        sessions = (
            db.query(ChargingSessionDB)
            .filter(ChargingSessionDB.charger_id == charger_id)
            .all()
        )

        if not sessions:
            raise HTTPException(status_code=404, detail="Sessions for that charger ID not found")

        return [
            {
                "id": s.id,
                "start_time": s.start_time,
                "end_time": s.end_time,
                "energy_delivered_kwh": s.energy_delivered_kwh,
            }
            for s in sessions
        ]
    finally:
        db.close()



