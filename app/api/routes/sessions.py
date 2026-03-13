from fastapi import APIRouter, Depends, HTTPException
from uuid import UUID

from app.db.session import SessionLocal
from app.models import Actions, Chargers, ChargingSessions, Owners
from app.schemas.database import ActionsRead, ChargingSessionsRead
from app.services.common.auth import TokenData, get_current_user
from app.services.common.authorization import (
    ensure_charger_access,
    ensure_session_access,
    get_accessible_charger_ids,
    is_admin,
    require_roles,
)

router = APIRouter(prefix="/sessions", tags=["sessions"])

@router.get("/all_sessions")
def get_all_sessions(current_user: TokenData = Depends(get_current_user)):
    """
    Return all charging sessions stored in the DB
    """
    db = SessionLocal()
    try:
        sessions = db.query(ChargingSessions).all()
        if not is_admin(current_user):
            charger_ids = set(get_accessible_charger_ids(db, current_user, allow_guest=False))
            sessions = [s for s in sessions if s.id_charger in charger_ids]

        return [
            {
                "id": s.id,
                "charger_id": s.id_charger,
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
def get_charger_sessions(charger_id: str, current_user: TokenData = Depends(get_current_user)):
    """
    Return all sessions of an specific charger stored in the DB
    """
    db = SessionLocal()
    try:
        ensure_charger_access(
            db,
            current_user,
            charger_id,
            allow_charger_owner=True,
            allow_pilot_owner=True,
            allow_guest=False,
        )

        sessions = (
            db.query(ChargingSessions)
            .filter(ChargingSessions.id_charger == charger_id)
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


@router.get("/owner/{owner_id}", response_model=list[ChargingSessionsRead])
def list_sessions_by_owner(owner_id: UUID, current_user: TokenData = Depends(get_current_user)):
    db = SessionLocal()
    try:
        if not is_admin(current_user):
            require_roles(current_user, "user")
            if current_user.owner_id != owner_id:
                raise HTTPException(status_code=403, detail="Forbidden")

        owner = db.query(Owners).filter(Owners.id == owner_id).first()
        if not owner:
            raise HTTPException(status_code=404, detail="Owner not found")

        return (
            db.query(ChargingSessions)
            .join(Chargers, ChargingSessions.id_charger == Chargers.id)
            .filter(Chargers.id_owner == owner_id)
            .all()
        )
    finally:
        db.close()


@router.get("/actions/session/{session_id}", response_model=list[ActionsRead])
def list_actions_by_session(session_id: UUID, current_user: TokenData = Depends(get_current_user)):
    db = SessionLocal()
    try:
        ensure_session_access(db, current_user, session_id, allow_guest=False)
        return db.query(Actions).filter(Actions.id_cs == session_id).all()
    finally:
        db.close()



