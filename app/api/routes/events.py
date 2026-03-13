from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from datetime import datetime
from uuid import uuid4
import json


from app.db.session import SessionLocal
from app.models import Chargers, ChargingSessions, Actions, Pilot, Owners

from app.services.ev_forecast.query import get_ev_forecast
from app.services.load_forecast.load_forecaster_service import forecast_load
from app.services.orchestrator.orchestrator_service import compute_and_save_action
from app.services.ev_forecast.updater import update_ev_forecast
from app.services.orchestrator.duration_cdf.updater import update_ev_duration_cdf
from app.services.common.auth import TokenData, get_current_user
from app.services.common.authorization import (
    ensure_charger_access,
    ensure_pilot_access,
    get_accessible_charger_ids,
    is_admin,
    require_admin_or_owner,
    require_roles,
)

router = APIRouter(prefix="/events", tags=["events"])

class VehicleConnectedEvent(BaseModel):
    charger_id: str
    timestamp: datetime
    measured_power_kw: float
    is_fully_charged: bool

class ChargingUpdateEvent(BaseModel):
    charger_id: str
    timestamp: datetime
    avg_power_last_15min_kw: float
    energy_delivered_kwh: float
    is_fully_charged: bool

class VehicleDisconnectedEvent(BaseModel):
    charger_id: str
    timestamp: datetime
    avg_power_last_15min_kw: float
    energy_delivered_kwh: float
    is_fully_charged: bool

@router.post("/vehicle_connected")
def vehicle_connected(
    event: VehicleConnectedEvent,
    current_user: TokenData = Depends(get_current_user),
):
    """
    Called when a vehicle plugs in.
    """
    db = SessionLocal()
    try:
        # 0. Check charger exists
        charger = db.query(Chargers).filter(Chargers.id == event.charger_id).first()
        if not charger:
            raise HTTPException(status_code=404, detail="Charger not found. You need to register it before starting a session, use create_charger endpoint.")
        ensure_charger_access(
            db,
            current_user,
            charger.id,
            allow_charger_owner=True,
            allow_pilot_owner=False,
            allow_guest=False,
        )

        # 0b. Identify pilot for this charger and count its chargers
        pilot = None
        controlled_charging_points = 1  # default to 1 if no pilot
        if charger.id_pilot:
            pilot = db.query(Pilot).filter(Pilot.id == charger.id_pilot).first()
            # Count chargers associated with this pilot
            if pilot:
                controlled_charging_points = db.query(Chargers).filter(Chargers.id_pilot == pilot.id).count()

        # 1. Check for active session in DB
        existing_session = (
            db.query(ChargingSessions)
            .filter(ChargingSessions.id_charger == charger.id, ChargingSessions.active == True)
            .with_for_update()
            .first()
        )

        # 2. Call load forecaster
        community_load_kw = forecast_load()

        if existing_session:
            # Active session found: compute action based on existing session and send warning

            current_power_kw = event.measured_power_kw
            energy_delivered_kwh = existing_session.energy_delivered_kwh
            is_fully_charged = event.is_fully_charged

            action = compute_and_save_action(
                db=db,
                session_id=existing_session.id,
                charger_id=event.charger_id,
                timestamp=event.timestamp,
                current_power_kw=current_power_kw,
                forecasted_energy_kwh=existing_session.forecasted_energy_kwh,
                forecasted_duration_hours=existing_session.forecasted_duration_hours,
                energy_delivered_kwh=energy_delivered_kwh,
                community_load_kw=community_load_kw,
                is_fully_charged=is_fully_charged,
                controlled_charging_points=existing_session.controlled_charging_points,
                time_connection=existing_session.start_time,
                forecasted_energy_kwh_std=existing_session.forecasted_energy_kwh_std,
                forecasted_duration_hours_std=existing_session.forecasted_duration_hours_std,
            )
            db.commit()

            return {
                "warning": f"Session already active ({existing_session.id}) for this charger. Please check and use charging_update endpoint for getting new actions.",
                "session_id": str(existing_session.id),
                "action": action,
            }

        # No active session found: create a new one and persist it
        # 1. Call EV forecaster (duration and energy)
        forecasted_energy_kwh, forecasted_energy_kwh_std, forecasted_duration_hours, forecasted_duration_hours_std = get_ev_forecast(
            charger_id=event.charger_id,
            start_time=event.timestamp
        )

        # create DB charging session (initial placeholders for end times and duration)
        new_session = ChargingSessions(
            id=uuid4(),
            start_time=event.timestamp,
            # end_time=event.timestamp,
            # end_charging_time=event.timestamp,
            # duration=0.0,
            energy_delivered_kwh=0.0,
            forecasted_energy_kwh=forecasted_energy_kwh,
            forecasted_energy_kwh_std=forecasted_energy_kwh_std,
            forecasted_duration_hours=forecasted_duration_hours,
            forecasted_duration_hours_std=forecasted_duration_hours_std,
            controlled_charging_points=controlled_charging_points,
            active=True,
            id_charger=charger.id,
        )
        db.add(new_session)
        db.flush()  # Send to DB to get ID, but don't commit yet
        db.refresh(new_session)

        # Call orchestrator
        action = compute_and_save_action(
            db=db,
            session_id=new_session.id,
            charger_id=event.charger_id,
            timestamp=event.timestamp,
            current_power_kw=event.measured_power_kw,
            forecasted_energy_kwh=new_session.forecasted_energy_kwh,
            forecasted_duration_hours=new_session.forecasted_duration_hours,
            energy_delivered_kwh=new_session.energy_delivered_kwh,
            community_load_kw=community_load_kw,
            is_fully_charged=event.is_fully_charged,
            controlled_charging_points=new_session.controlled_charging_points,
            time_connection=new_session.start_time,
            forecasted_energy_kwh_std=new_session.forecasted_energy_kwh_std,
            forecasted_duration_hours_std=new_session.forecasted_duration_hours_std,
        )
        db.commit()

        return {"action": action, "session_id": str(new_session.id)}

    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@router.post("/charging_update")
def charging_update(
    event: ChargingUpdateEvent,
    current_user: TokenData = Depends(get_current_user),
):
    """
    Called when a new action for the charger is needed
    """

    db = SessionLocal()
    try:
        # 1. Retrieve active session
        session = (
            db.query(ChargingSessions)
            .filter(ChargingSessions.id_charger == event.charger_id, ChargingSessions.active == True)
            .with_for_update()
            .first()
        )

        if session is None:
            raise HTTPException(status_code=404, detail=f"No active session for charger {event.charger_id}")

        ensure_charger_access(
            db,
            current_user,
            session.id_charger,
            allow_charger_owner=True,
            allow_pilot_owner=False,
            allow_guest=False,
        )

        # 1b. Get charger and identify pilot for this charger
        charger = db.query(Chargers).filter(Chargers.id == event.charger_id).first()
        pilot = None
        if charger and charger.id_pilot:
            pilot = db.query(Pilot).filter(Pilot.id == charger.id_pilot).first()

        # 2. Update session dynamic state
        session.energy_delivered_kwh += event.energy_delivered_kwh
        # session.avg_measured_power_kw = event.avg_power_last_15min_kw
        session.is_fully_charged = event.is_fully_charged
        if session.is_fully_charged == True or event.avg_power_last_15min_kw < 0.01:
            session.end_charging_time = event.timestamp

        # 3. Call load forecaster
        community_load_kw = forecast_load()

        # 4. Call orchestrator
        action = compute_and_save_action(
            db=db,
            session_id=session.id,
            charger_id= session.id_charger,
            timestamp = event.timestamp,
            current_power_kw=event.avg_power_last_15min_kw,
            forecasted_energy_kwh=session.forecasted_energy_kwh,
            forecasted_duration_hours=session.forecasted_duration_hours,
            energy_delivered_kwh=session.energy_delivered_kwh,
            community_load_kw=community_load_kw,
            is_fully_charged=event.is_fully_charged,
            controlled_charging_points=session.controlled_charging_points,
            time_connection = session.start_time,
            forecasted_energy_kwh_std = session.forecasted_energy_kwh_std,
            forecasted_duration_hours_std = session.forecasted_duration_hours_std,
        )

        db.commit()
        return {"action": action}

    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@router.post("/vehicle_disconnected")
def vehicle_disconnected(
    event: VehicleDisconnectedEvent,
    current_user: TokenData = Depends(get_current_user),
):
    """
    Called when a vehicles disconnects
    """
    db = SessionLocal()
    try:
        # 1. Retrieve active session
        session = (
            db.query(ChargingSessions)
            .filter(ChargingSessions.id_charger == event.charger_id, ChargingSessions.active == True)
            .with_for_update()
            .first()
        )
        if session is None:
            raise HTTPException(status_code=404, detail=f"No active session for charger {event.charger_id}")

        ensure_charger_access(
            db,
            current_user,
            session.id_charger,
            allow_charger_owner=True,
            allow_pilot_owner=False,
            allow_guest=False,
        )

        # 2. Update session final state
        session.energy_delivered_kwh = session.energy_delivered_kwh + event.energy_delivered_kwh
        if session.end_charging_time is None:
            session.end_charging_time = event.timestamp
        session.end_time = event.timestamp
        session.duration = (session.end_time - session.start_time).total_seconds()/3600
        session.active = False

        # persist changes (but don't commit yet)
        db.flush()

        session_summary = {
            "charger_id": session.id_charger,
            "start_time": session.start_time,
            "disconnection_time": event.timestamp,
            "end_charging_time": session.end_charging_time,
            "charged_energy_kwh": session.energy_delivered_kwh,
            "forecasted_energy_kwh": session.forecasted_energy_kwh,
            "forecasted_duration_hours": session.forecasted_duration_hours,
        }

        print("Session closed:", session_summary)

        # 3. Update forecasting models (use persisted session data)
        duration_hours = session.duration

        update_ev_forecast(
            db=db,
            charger_id=session.id_charger,
            start_time=session.start_time,
            energy_kwh=session.energy_delivered_kwh,
            duration_hours=duration_hours,
        )

        update_ev_duration_cdf(
            db=db,
            charger_id=session.id_charger,
            start_time=session.start_time,
            duration_hours=duration_hours,
        )

        # Commit all changes atomically (session closure + both forecasting updates)
        db.commit()

        return {
            "status": "session closed and stored",
            "session_summary": session_summary,
        }

    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@router.get("/active_sessions")
def get_active_sessions(current_user: TokenData = Depends(get_current_user)):
    """
    Return all charging sessions currently active
    """
    db = SessionLocal()
    try:
        sessions = (
            db.query(ChargingSessions)
            .filter(ChargingSessions.active == True)
            .all()
        )

        if not is_admin(current_user):
            charger_ids = set(get_accessible_charger_ids(db, current_user, allow_guest=False))
            sessions = [s for s in sessions if s.id_charger in charger_ids]

        result = {}
        for s in sessions:
            # latest action for this session (if any)
            latest_action = (
                db.query(Actions)
                .filter(Actions.id_cs == s.id)
                .order_by(Actions.current_time.desc())
                .first()
            )

            avg_measured_power = latest_action.current_power_kw if latest_action else None
            is_fully = latest_action.is_fully_charged if latest_action else None
            last_update = latest_action.current_time if latest_action else s.updated_at

            result[str(s.id)] = {
                "charger_id": (str(s.id_charger) if s.id_charger else None),
                "start_time": s.start_time,
                "energy_delivered_kwh": s.energy_delivered_kwh,
                "forecasted_energy_kwh": s.forecasted_energy_kwh,
                "forecasted_duration_hours": s.forecasted_duration_hours,
                "avg_measured_power_kw": avg_measured_power,
                "is_fully_charged": is_fully,
                "last_update_time": last_update,
            }

        return result
    finally:
        db.close()

@router.get("/active_sessions/pilot/{pilot_id}")
def get_active_sessions_by_pilot(
    pilot_id: str,
    current_user: TokenData = Depends(get_current_user),
):
    """
    Return all charging sessions currently active for a specific pilot
    """
    db = SessionLocal()
    try:
        # Verify pilot exists
        pilot = ensure_pilot_access(
            db,
            current_user,
            pilot_id,
            allow_user=True,
            allow_guest=False,
            require_user_ownership=True,
        )

        # Get active sessions for chargers associated with this pilot
        sessions = (
            db.query(ChargingSessions)
            .join(Chargers, ChargingSessions.id_charger == Chargers.id)
            .filter(Chargers.id_pilot == pilot_id, ChargingSessions.active == True)
            .all()
        )

        result = {}
        for s in sessions:
            # latest action for this session (if any)
            latest_action = (
                db.query(Actions)
                .filter(Actions.id_cs == s.id)
                .order_by(Actions.current_time.desc())
                .first()
            )

            avg_measured_power = latest_action.current_power_kw if latest_action else None
            is_fully = latest_action.is_fully_charged if latest_action else None
            last_update = latest_action.current_time if latest_action else s.updated_at

            result[str(s.id)] = {
                "charger_id": (str(s.id_charger) if s.id_charger else None),
                "start_time": s.start_time,
                "energy_delivered_kwh": s.energy_delivered_kwh,
                "forecasted_energy_kwh": s.forecasted_energy_kwh,
                "forecasted_duration_hours": s.forecasted_duration_hours,
                "avg_measured_power_kw": avg_measured_power,
                "is_fully_charged": is_fully,
                "last_update_time": last_update,
            }

        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@router.get("/active_sessions/owner/{owner_id}")
def get_active_sessions_by_owner(
    owner_id: str,
    current_user: TokenData = Depends(get_current_user),
):
    """
    Return all charging sessions currently active for a specific owner
    """
    db = SessionLocal()
    try:
        # Verify owner exists
        owner = db.query(Owners).filter(Owners.id == owner_id).first()
        if not owner:
            raise HTTPException(status_code=404, detail=f"Owner {owner_id} not found")

        if not is_admin(current_user):
            require_roles(current_user, "user")
        require_admin_or_owner(current_user, owner.id)

        # Get active sessions for chargers associated with this owner
        sessions = (
            db.query(ChargingSessions)
            .join(Chargers, ChargingSessions.id_charger == Chargers.id)
            .filter(Chargers.id_owner == owner_id, ChargingSessions.active == True)
            .all()
        )

        result = {}
        for s in sessions:
            # latest action for this session (if any)
            latest_action = (
                db.query(Actions)
                .filter(Actions.id_cs == s.id)
                .order_by(Actions.current_time.desc())
                .first()
            )

            avg_measured_power = latest_action.current_power_kw if latest_action else None
            is_fully = latest_action.is_fully_charged if latest_action else None
            last_update = latest_action.current_time if latest_action else s.updated_at

            result[str(s.id)] = {
                "charger_id": (str(s.id_charger) if s.id_charger else None),
                "start_time": s.start_time,
                "energy_delivered_kwh": s.energy_delivered_kwh,
                "forecasted_energy_kwh": s.forecasted_energy_kwh,
                "forecasted_duration_hours": s.forecasted_duration_hours,
                "avg_measured_power_kw": avg_measured_power,
                "is_fully_charged": is_fully,
                "last_update_time": last_update,
            }

        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

