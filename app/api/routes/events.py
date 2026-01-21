from fastapi import APIRouter
from pydantic import BaseModel
from datetime import datetime

from app.services.session_state import ChargingSessionState
from app.services.session_store import ACTIVE_SESSIONS

from app.services.ev_forecast.query import get_ev_forecast
from app.services.load_forecast.load_forecaster_service import forecast_load
from app.services.orchestrator.orchestrator_service import compute_action
from app.services.session_service import store_completed_session
from app.services.ev_forecast.updater import update_ev_forecast
from app.services.orchestrator.duration_cdf.updater import update_ev_duration_cdf

router = APIRouter(prefix="/events", tags=["events"])

class VehicleConnectedEvent(BaseModel):
    charger_id: str
    timestamp: datetime
    charger_type: str
    measured_power_kw: float
    charger_nom_power_kw: float
    is_fully_charged: bool
    controlled_charging_points: int

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
def vehicle_connected(event: VehicleConnectedEvent):
    """
    Called when a vehicle plugs in.
    """

    # 0. Check if session already exists
    existing_session = ACTIVE_SESSIONS.get(event.charger_id)

    if existing_session:
        # Idempotent behavior: return current action
        community_load_kw = forecast_load()

        action = compute_action(
            charger_id=event.charger_id,
            timestamp=event.timestamp,
            current_power_kw=existing_session.avg_measured_power_kw,
            forecasted_energy_kwh=existing_session.forecasted_energy_kwh,
            forecasted_duration_hours=existing_session.forecasted_duration_hours,
            energy_delivered_kwh=existing_session.energy_delivered_kwh,
            community_load_kw=community_load_kw,
            is_fully_charged=existing_session.is_fully_charged,
            controlled_charging_points = existing_session.controlled_charging_points,
            time_connection=existing_session.start_time,
            forecasted_energy_kwh_std=existing_session.forecasted_energy_kwh_std,
            forecasted_duration_hours_std=existing_session.forecasted_duration_hours_std,
        )

        return {
            "warning": "Vehicle already connected",
            "action": action,
        }


    # 1. Call EV forecaster (ONCE per session)
    forecasted_energy_kwh, forecasted_energy_kwh_std, forecasted_duration_hours, forecasted_duration_hours_std = get_ev_forecast(
        charger_id=event.charger_id,
        start_time=event.timestamp
    )

    # 2. Call load forecaster (current state)
    community_load_kw = forecast_load()

    #Create session state
    session = ChargingSessionState(
        charger_id=event.charger_id,
        start_time=event.timestamp,
        charged_energy_kwh=0.0,
        avg_measured_power_kw=event.measured_power_kw,
        is_fully_charged=event.is_fully_charged,
        forecasted_energy_kwh=forecasted_energy_kwh,
        forecasted_energy_kwh_std=forecasted_energy_kwh_std,
        forecasted_duration_hours=forecasted_duration_hours,
        forecasted_duration_hours_std=forecasted_duration_hours_std,
        controlled_charging_points= event.controlled_charging_points
    )

    ACTIVE_SESSIONS[event.charger_id] = session

    # Call orchestrator
    action = compute_action(
        charger_id = event.charger_id,
        timestamp = event.timestamp,
        current_power_kw = event.measured_power_kw,
        forecasted_energy_kwh=session.forecasted_energy_kwh,
        forecasted_duration_hours=session.forecasted_duration_hours,
        energy_delivered_kwh= session.energy_delivered_kwh,
        community_load_kw= community_load_kw,
        is_fully_charged=session.is_fully_charged,
        controlled_charging_points= session.controlled_charging_points,
        time_connection = session.start_time,
        forecasted_energy_kwh_std = session.forecasted_energy_kwh_std,
        forecasted_duration_hours_std = session.forecasted_duration_hours_std,
    )

    return {"action": action}

@router.post("/charging_update")
def charging_update(event: ChargingUpdateEvent):
    """
    Called when a new action for the charger is needed
    """
    # 1. Retrieve active session
    session = ACTIVE_SESSIONS.get(event.charger_id)

    if session is None:
        return {"error": f"No active session for charger {event.charger_id}"}

    # 2. Update session dynamic state
    session.energy_delivered_kwh += event.energy_delivered_kwh
    session.avg_measured_power_kw = event.avg_power_last_15min_kw
    session.is_fully_charged = event.is_fully_charged
    session.last_update_time = event.timestamp
    if session.is_fully_charged == True or event.avg_power_last_15min_kw < 0.01:
        session.end_charging_time = event.timestamp

    # 3. Call load forecaster
    community_load_kw = forecast_load()

    # 4. Call orchestrator

    action = compute_action(
        charger_id= session.charger_id,
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

    return {"action": action}

@router.post("/vehicle_disconnected")
def vehicle_disconnected(event: VehicleDisconnectedEvent):
    """
    Called when a vehicles disconnects
    """
    # 1. Retrieve active session
    session = ACTIVE_SESSIONS.get(event.charger_id)

    if session is None:
        return {"error": f"No active session for charger {event.charger_id}"}

    # 2. Update session dynamic state
    session.energy_delivered_kwh += event.energy_delivered_kwh
    session.avg_measured_power_kw = event.avg_power_last_15min_kw
    session.is_fully_charged = event.is_fully_charged
    session.last_update_time = event.timestamp
    if session.end_charging_time is None:
        session.end_charging_time = event.timestamp

    session_summary = {
        "charger_id": session.charger_id,
        "start_time": session.start_time,
        "disconnection_time": event.timestamp,
        # "end_charging_time": session.get("end_charging_time", event.timestamp),
        "charged_energy_kwh": session.energy_delivered_kwh,
        "forecasted_energy_kwh": session.forecasted_energy_kwh,
        "forecasted_duration_hours": session.forecasted_duration_hours,
    }

    # 3. Send to EV forecaster (placeholder)
    # ev_forecaster.update(session_summary)

    print("Session closed:", session_summary)

    # Persist completed session
    store_completed_session(session)

    # Update the ev forecasters
    update_ev_forecast(
        charger_id=session.charger_id,
        start_time=session.start_time,
        energy_kwh=session.energy_delivered_kwh,
        duration_hours= (event.timestamp - session.start_time).total_seconds() / 3600
    )

    #Update the duration cdf database
    update_ev_duration_cdf(
            charger_id = session.charger_id,
            start_time= session.start_time,
            duration_hours= (event.timestamp - session.start_time).total_seconds() / 3600,
    )

    # 4. Remove from active sessions
    del ACTIVE_SESSIONS[event.charger_id]

    return {
        "status": "session closed and stored",
        "session_summary": session_summary
    }

@router.get("/active_sessions")
def get_active_sessions():
    """
    Return all charging sessions currently active
    """
    return {
        charger_id: {
            "charger_id": s.charger_id,
            "start_time": s.start_time,
            "energy_delivered_kwh": s.energy_delivered_kwh,
            "forecasted_energy_kwh": s.forecasted_energy_kwh,
            "forecasted_duration_hours": s.forecasted_duration_hours,
            "avg_measured_power_kw": s.avg_measured_power_kw,
            "is_fully_charged": s.is_fully_charged,
            "last_update_time": s.last_update_time,
        }
        for charger_id, s in ACTIVE_SESSIONS.items()
    }

