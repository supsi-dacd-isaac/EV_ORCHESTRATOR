from fastapi import APIRouter, HTTPException
from uuid import UUID, uuid4
from datetime import datetime

from app.db.session import SessionLocal
from app.models import Owners, Chargers, ChargingSessions, Actions, EvDurationCdf, EvForecastStats, Pilot, GridLoadForecasted
from app.schemas.database import (
    OwnersCreate, OwnersUpdate, OwnersRead,
    ChargersCreate, ChargersUpdate, ChargersRead,
    ChargingSessionsCreate, ChargingSessionsUpdate, ChargingSessionsRead,
    ActionsCreate, ActionsUpdate, ActionsRead,
    EvDurationCdfCreate, EvDurationCdfUpdate, EvDurationCdfRead,
    EvForecastStatsCreate, EvForecastStatsUpdate, EvForecastStatsRead,
    PilotCreate, PilotUpdate, PilotRead,
    GridLoadForecastedCreate, GridLoadForecastedUpdate, GridLoadForecastedRead,
)

router = APIRouter(prefix="/db", tags=["database"])

# ---------- Owners ----------
@router.post("/owners", response_model=OwnersRead)
def create_owner(payload: OwnersCreate):
    db = SessionLocal()
    try:
        existing = db.query(Owners).filter(Owners.user == payload.user).first()
        if existing:
            raise HTTPException(status_code=400, detail="User already exists")

        obj = Owners(
            id=uuid4(),
            user=payload.user,
            password=payload.password,
            company_name=payload.company_name,
            type=payload.type,
            role=payload.role,
        )
        db.add(obj)
        db.commit()
        db.refresh(obj)
        return obj
    finally:
        db.close()


@router.get("/owners", response_model=list[OwnersRead])
def list_owners():
    db = SessionLocal()
    try:
        return db.query(Owners).all()
    finally:
        db.close()


@router.get("/owners/{owner_id}", response_model=OwnersRead)
def get_owner(owner_id: UUID):
    db = SessionLocal()
    try:
        obj = db.query(Owners).filter(Owners.id == owner_id).first()
        if not obj:
            raise HTTPException(status_code=404, detail="Owner not found")
        return obj
    finally:
        db.close()


@router.put("/owners/{owner_id}", response_model=OwnersRead)
def update_owner(owner_id: UUID, payload: OwnersUpdate):
    db = SessionLocal()
    try:
        obj = db.query(Owners).filter(Owners.id == owner_id).first()
        if not obj:
            raise HTTPException(status_code=404, detail="Owner not found")

        data = payload.dict(exclude_unset=True)
        for k, v in data.items():
            setattr(obj, k, v)
        db.commit()
        db.refresh(obj)
        return obj
    finally:
        db.close()


@router.delete("/owners/{owner_id}")
def delete_owner(owner_id: UUID):
    db = SessionLocal()
    try:
        obj = db.query(Owners).filter(Owners.id == owner_id).first()
        if not obj:
            raise HTTPException(status_code=404, detail="Owner not found")
        db.delete(obj)
        db.commit()
        return {"ok": True}
    finally:
        db.close()


# ---------- Chargers ----------
@router.post("/chargers", response_model=ChargersRead)
def create_charger(payload: ChargersCreate):
    db = SessionLocal()
    try:
        obj = Chargers(
            id=uuid4(),
            name=payload.name,
            type=payload.type,
            latitude=payload.latitude,
            longitude=payload.longitude,
            nominal_power=payload.nominal_power,
            plugs=payload.plugs,
            id_owner=payload.id_owner,
            id_pilot=payload.id_pilot,
        )
        db.add(obj)
        db.commit()
        db.refresh(obj)
        return obj
    finally:
        db.close()


@router.get("/chargers", response_model=list[ChargersRead])
def list_chargers():
    db = SessionLocal()
    try:
        return db.query(Chargers).all()
    finally:
        db.close()


@router.get("/chargers/{charger_id}", response_model=ChargersRead)
def get_charger(charger_id: UUID):
    db = SessionLocal()
    try:
        obj = db.query(Chargers).filter(Chargers.id == charger_id).first()
        if not obj:
            raise HTTPException(status_code=404, detail="Charger not found")
        return obj
    finally:
        db.close()


@router.put("/chargers/{charger_id}", response_model=ChargersRead)
def update_charger(charger_id: UUID, payload: ChargersUpdate):
    db = SessionLocal()
    try:
        obj = db.query(Chargers).filter(Chargers.id == charger_id).first()
        if not obj:
            raise HTTPException(status_code=404, detail="Charger not found")

        data = payload.dict(exclude_unset=True)
        # Exclude updated_at from being set explicitly (let DB handle it)
        data.pop('updated_at', None)
        for k, v in data.items():
            setattr(obj, k, v)
        db.commit()
        db.refresh(obj)
        return obj
    finally:
        db.close()


@router.delete("/chargers/{charger_id}")
def delete_charger(charger_id: UUID):
    db = SessionLocal()
    try:
        obj = db.query(Chargers).filter(Chargers.id == charger_id).first()
        if not obj:
            raise HTTPException(status_code=404, detail="Charger not found")
        db.delete(obj)
        db.commit()
        return {"ok": True}
    finally:
        db.close()


# ---------- Charging Sessions ----------
@router.post("/sessions", response_model=ChargingSessionsRead)
def create_session(payload: ChargingSessionsCreate):
    db = SessionLocal()
    try:
        obj = ChargingSessions(
            id=uuid4(),
            start_time=payload.start_time,
            energy_delivered_kwh=payload.energy_delivered_kwh,
            forecasted_energy_kwh=payload.forecasted_energy_kwh,
            forecasted_energy_kwh_std=payload.forecasted_energy_kwh_std,
            forecasted_duration_hours=payload.forecasted_duration_hours,
            forecasted_duration_hours_std=payload.forecasted_duration_hours_std,
            controlled_charging_points=payload.controlled_charging_points,
            active=payload.active,
            id_charger=payload.id_charger,
            end_time=payload.end_time,
            end_charging_time=payload.end_charging_time,
            duration=payload.duration,
        )
        db.add(obj)
        db.commit()
        db.refresh(obj)
        return obj
    finally:
        db.close()


@router.get("/sessions", response_model=list[ChargingSessionsRead])
def list_sessions():
    db = SessionLocal()
    try:
        return db.query(ChargingSessions).all()
    finally:
        db.close()


@router.get("/sessions/{session_id}", response_model=ChargingSessionsRead)
def get_session(session_id: UUID):
    db = SessionLocal()
    try:
        obj = db.query(ChargingSessions).filter(ChargingSessions.id == session_id).first()
        if not obj:
            raise HTTPException(status_code=404, detail="Session not found")
        return obj
    finally:
        db.close()


@router.put("/sessions/{session_id}", response_model=ChargingSessionsRead)
def update_session(session_id: UUID, payload: ChargingSessionsUpdate):
    db = SessionLocal()
    try:
        obj = db.query(ChargingSessions).filter(ChargingSessions.id == session_id).first()
        if not obj:
            raise HTTPException(status_code=404, detail="Session not found")

        data = payload.dict(exclude_unset=True)
        # Exclude updated_at from being set explicitly (let DB handle it)
        data.pop('updated_at', None)
        for k, v in data.items():
            setattr(obj, k, v)
        obj.updated_at = data.get('updated_at', datetime.utcnow())
        db.commit()
        db.refresh(obj)
        return obj
    finally:
        db.close()


@router.delete("/sessions/{session_id}")
def delete_session(session_id: UUID):
    db = SessionLocal()
    try:
        obj = db.query(ChargingSessions).filter(ChargingSessions.id == session_id).first()
        if not obj:
            raise HTTPException(status_code=404, detail="Session not found")
        db.delete(obj)
        db.commit()
        return {"ok": True}
    finally:
        db.close()


# ---------- Pilot ----------
@router.post("/pilots", response_model=PilotRead)
def create_pilot(payload: PilotCreate):
    db = SessionLocal()
    try:
        obj = Pilot(
            id=uuid4(),
            name=payload.name,
            id_owner=payload.id_owner,
        )
        db.add(obj)
        db.commit()
        db.refresh(obj)
        return obj
    finally:
        db.close()


@router.get("/pilots", response_model=list[PilotRead])
def list_pilots():
    db = SessionLocal()
    try:
        return db.query(Pilot).all()
    finally:
        db.close()


@router.get("/pilots/{pilot_id}", response_model=PilotRead)
def get_pilot(pilot_id: UUID):
    db = SessionLocal()
    try:
        obj = db.query(Pilot).filter(Pilot.id == pilot_id).first()
        if not obj:
            raise HTTPException(status_code=404, detail="Pilot not found")
        return obj
    finally:
        db.close()


@router.put("/pilots/{pilot_id}", response_model=PilotRead)
def update_pilot(pilot_id: UUID, payload: PilotUpdate):
    db = SessionLocal()
    try:
        obj = db.query(Pilot).filter(Pilot.id == pilot_id).first()
        if not obj:
            raise HTTPException(status_code=404, detail="Pilot not found")

        data = payload.dict(exclude_unset=True)
        for k, v in data.items():
            setattr(obj, k, v)
        db.commit()
        db.refresh(obj)
        return obj
    finally:
        db.close()


@router.delete("/pilots/{pilot_id}")
def delete_pilot(pilot_id: UUID):
    db = SessionLocal()
    try:
        obj = db.query(Pilot).filter(Pilot.id == pilot_id).first()
        if not obj:
            raise HTTPException(status_code=404, detail="Pilot not found")
        db.delete(obj)
        db.commit()
        return {"ok": True}
    finally:
        db.close()

# ---------- Actions ----------
@router.post("/actions", response_model=ActionsRead)
def create_action(payload: ActionsCreate):
    db = SessionLocal()
    try:
        obj = Actions(
            id=uuid4(),
            current_time=payload.current_time,
            current_power_kw=payload.current_power_kw,
            energy_delivered_kwh=payload.energy_delivered_kwh,
            is_fully_charged=payload.is_fully_charged,
            probability_disconnection=payload.probability_disconnection,
            cumulative_duration_probability=payload.cumulative_duration_probability,
            community_load=payload.community_load,
            action=payload.action,
            id_cs=payload.id_cs,
        )
        db.add(obj)
        db.commit()
        db.refresh(obj)
        return obj
    finally:
        db.close()


@router.get("/actions", response_model=list[ActionsRead])
def list_actions():
    db = SessionLocal()
    try:
        return db.query(Actions).all()
    finally:
        db.close()


@router.get("/actions/{action_id}", response_model=ActionsRead)
def get_action(action_id: UUID):
    db = SessionLocal()
    try:
        obj = db.query(Actions).filter(Actions.id == action_id).first()
        if not obj:
            raise HTTPException(status_code=404, detail="Action not found")
        return obj
    finally:
        db.close()


@router.put("/actions/{action_id}", response_model=ActionsRead)
def update_action(action_id: UUID, payload: ActionsUpdate):
    db = SessionLocal()
    try:
        obj = db.query(Actions).filter(Actions.id == action_id).first()
        if not obj:
            raise HTTPException(status_code=404, detail="Action not found")

        data = payload.dict(exclude_unset=True)
        for k, v in data.items():
            setattr(obj, k, v)
        db.commit()
        db.refresh(obj)
        return obj
    finally:
        db.close()


@router.delete("/actions/{action_id}")
def delete_action(action_id: UUID):
    db = SessionLocal()
    try:
        obj = db.query(Actions).filter(Actions.id == action_id).first()
        if not obj:
            raise HTTPException(status_code=404, detail="Action not found")
        db.delete(obj)
        db.commit()
        return {"ok": True}
    finally:
        db.close()


# ---------- EV Duration CDF ----------
@router.post("/duration_cdf", response_model=EvDurationCdfRead)
def create_duration(payload: EvDurationCdfCreate):
    db = SessionLocal()
    try:
        obj = EvDurationCdf(
            id=uuid4(),
            hour=payload.hour,
            horizon_hours=payload.horizon_hours,
            probability=payload.probability,
            sample_count=payload.sample_count,
            updated_at=payload.updated_at,
            id_charger=payload.id_charger,
        )
        db.add(obj)
        db.commit()
        db.refresh(obj)
        return obj
    finally:
        db.close()


@router.get("/duration_cdf", response_model=list[EvDurationCdfRead])
def list_duration():
    db = SessionLocal()
    try:
        return db.query(EvDurationCdf).all()
    finally:
        db.close()


@router.get("/duration_cdf/{duration_id}", response_model=EvDurationCdfRead)
def get_duration(duration_id: UUID):
    db = SessionLocal()
    try:
        obj = db.query(EvDurationCdf).filter(EvDurationCdf.id == duration_id).first()
        if not obj:
            raise HTTPException(status_code=404, detail="Duration CDF not found")
        return obj
    finally:
        db.close()


@router.put("/duration_cdf/{duration_id}", response_model=EvDurationCdfRead)
def update_duration(duration_id: UUID, payload: EvDurationCdfUpdate):
    db = SessionLocal()
    try:
        obj = db.query(EvDurationCdf).filter(EvDurationCdf.id == duration_id).first()
        if not obj:
            raise HTTPException(status_code=404, detail="Duration CDF not found")

        data = payload.dict(exclude_unset=True)
        # Exclude updated_at from being set explicitly (let DB handle it)
        data.pop('updated_at', None)
        for k, v in data.items():
            setattr(obj, k, v)
        db.commit()
        db.refresh(obj)
        return obj
    finally:
        db.close()


@router.delete("/duration_cdf/{duration_id}")
def delete_duration(duration_id: UUID):
    db = SessionLocal()
    try:
        obj = db.query(EvDurationCdf).filter(EvDurationCdf.id == duration_id).first()
        if not obj:
            raise HTTPException(status_code=404, detail="Duration CDF not found")
        db.delete(obj)
        db.commit()
        return {"ok": True}
    finally:
        db.close()


# ---------- EV Forecast Stats ----------
@router.post("/ev_forecast_stats", response_model=EvForecastStatsRead)
def create_ev_forecast(payload: EvForecastStatsCreate):
    db = SessionLocal()
    try:
        obj = EvForecastStats(
            id=uuid4(),
            hour=payload.hour,
            mean_energy_kwh=payload.mean_energy_kwh,
            std_energy_kwh=payload.std_energy_kwh,
            mean_duration_hours=payload.mean_duration_hours,
            std_duration_hours=payload.std_duration_hours,
            sample_count=payload.sample_count,
            updated_at=payload.updated_at,
            id_charger=payload.id_charger,
        )
        db.add(obj)
        db.commit()
        db.refresh(obj)
        return obj
    finally:
        db.close()


@router.get("/ev_forecast_stats", response_model=list[EvForecastStatsRead])
def list_ev_forecast():
    db = SessionLocal()
    try:
        return db.query(EvForecastStats).all()
    finally:
        db.close()


@router.get("/ev_forecast_stats/{forecast_id}", response_model=EvForecastStatsRead)
def get_ev_forecast(forecast_id: UUID):
    db = SessionLocal()
    try:
        obj = db.query(EvForecastStats).filter(EvForecastStats.id == forecast_id).first()
        if not obj:
            raise HTTPException(status_code=404, detail="EV forecast stat not found")
        return obj
    finally:
        db.close()


@router.put("/ev_forecast_stats/{forecast_id}", response_model=EvForecastStatsRead)
def update_ev_forecast(forecast_id: UUID, payload: EvForecastStatsUpdate):
    db = SessionLocal()
    try:
        obj = db.query(EvForecastStats).filter(EvForecastStats.id == forecast_id).first()
        if not obj:
            raise HTTPException(status_code=404, detail="EV forecast stat not found")

        data = payload.dict(exclude_unset=True)
        # Exclude updated_at from being set explicitly (let DB handle it)
        data.pop('updated_at', None)
        for k, v in data.items():
            setattr(obj, k, v)
        db.commit()
        db.refresh(obj)
        return obj
    finally:
        db.close()


@router.delete("/ev_forecast_stats/{forecast_id}")
def delete_ev_forecast(forecast_id: UUID):
    db = SessionLocal()
    try:
        obj = db.query(EvForecastStats).filter(EvForecastStats.id == forecast_id).first()
        if not obj:
            raise HTTPException(status_code=404, detail="EV forecast stat not found")
        db.delete(obj)
        db.commit()
        return {"ok": True}
    finally:
        db.close()


# ---------- Grid Load Forecasted ----------
@router.post("/grid_load_forecasted", response_model=GridLoadForecastedRead)
def create_grid_load_forecasted(payload: GridLoadForecastedCreate):
    db = SessionLocal()
    try:
        obj = GridLoadForecasted(
            id=uuid4(),
            value=payload.value,
            id_action=payload.id_action,
        )
        db.add(obj)
        db.commit()
        db.refresh(obj)
        return obj
    finally:
        db.close()


@router.get("/grid_load_forecasted", response_model=list[GridLoadForecastedRead])
def list_grid_load_forecasted():
    db = SessionLocal()
    try:
        return db.query(GridLoadForecasted).all()
    finally:
        db.close()


@router.get("/grid_load_forecasted/{grid_load_id}", response_model=GridLoadForecastedRead)
def get_grid_load_forecasted(grid_load_id: UUID):
    db = SessionLocal()
    try:
        obj = db.query(GridLoadForecasted).filter(GridLoadForecasted.id == grid_load_id).first()
        if not obj:
            raise HTTPException(status_code=404, detail="Grid load forecasted not found")
        return obj
    finally:
        db.close()


@router.put("/grid_load_forecasted/{grid_load_id}", response_model=GridLoadForecastedRead)
def update_grid_load_forecasted(grid_load_id: UUID, payload: GridLoadForecastedUpdate):
    db = SessionLocal()
    try:
        obj = db.query(GridLoadForecasted).filter(GridLoadForecasted.id == grid_load_id).first()
        if not obj:
            raise HTTPException(status_code=404, detail="Grid load forecasted not found")

        data = payload.dict(exclude_unset=True)
        for k, v in data.items():
            setattr(obj, k, v)
        db.commit()
        db.refresh(obj)
        return obj
    finally:
        db.close()


@router.delete("/grid_load_forecasted/{grid_load_id}")
def delete_grid_load_forecasted(grid_load_id: UUID):
    db = SessionLocal()
    try:
        obj = db.query(GridLoadForecasted).filter(GridLoadForecasted.id == grid_load_id).first()
        if not obj:
            raise HTTPException(status_code=404, detail="Grid load forecasted not found")
        db.delete(obj)
        db.commit()
        return {"ok": True}
    finally:
        db.close()
