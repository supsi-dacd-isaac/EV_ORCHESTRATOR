from fastapi import APIRouter, Depends, HTTPException
from uuid import UUID, uuid4
from datetime import datetime

from app.db.session import SessionLocal
from app.models import Owners, Chargers, ChargingSessions, Actions, EvDurationCdf, EvForecastStats, Pilot, GridLoadForecasted, ForecastJobDB
from app.schemas.database import (
    OwnersCreate, OwnersUpdate, OwnersRead,
    ChargersCreate, ChargersUpdate, ChargersRead,
    ChargingSessionsCreate, ChargingSessionsUpdate, ChargingSessionsRead,
    ActionsCreate, ActionsUpdate, ActionsRead,
    EvDurationCdfCreate, EvDurationCdfUpdate, EvDurationCdfRead,
    EvForecastStatsCreate, EvForecastStatsUpdate, EvForecastStatsRead,
    PilotCreate, PilotUpdate, PilotRead,
    GridLoadForecastedCreate, GridLoadForecastedUpdate, GridLoadForecastedRead,
    ForecastJobCreate, ForecastJobUpdate, ForecastJobRead,
)
from app.services.common.auth import TokenData, get_current_user, hash_password
from app.services.common.authorization import (
    ensure_action_access,
    ensure_charger_access,
    ensure_pilot_access,
    ensure_session_access,
    get_accessible_charger_ids,
    get_role,
    is_admin,
    is_user_role,
    require_admin,
    require_admin_or_owner,
    require_roles,
)

router = APIRouter(prefix="/db", tags=["database"])

# ---------- Owners ----------
@router.post("/owners", response_model=OwnersRead)
def create_owner(payload: OwnersCreate, current_user: TokenData = Depends(get_current_user)):
    db = SessionLocal()
    try:
        require_admin(current_user)
        existing = db.query(Owners).filter(Owners.user == payload.user).first()
        if existing:
            raise HTTPException(status_code=400, detail="User already exists")

        obj = Owners(
            id=uuid4(),
            user=payload.user,
            password=hash_password(payload.password),
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


@router.get("/owners", response_model=list[OwnersRead], response_model_exclude={"password"})
def list_owners(current_user: TokenData = Depends(get_current_user)):
    db = SessionLocal()
    try:
        require_roles(current_user, "admin", "user", "guest")
        return db.query(Owners).all()
    finally:
        db.close()


@router.get("/owners/{owner_id}", response_model=OwnersRead, response_model_exclude={"password"})
def get_owner(owner_id: UUID, current_user: TokenData = Depends(get_current_user)):
    db = SessionLocal()
    try:
        require_roles(current_user, "admin", "user", "guest")
        obj = db.query(Owners).filter(Owners.id == owner_id).first()
        if not obj:
            raise HTTPException(status_code=404, detail="Owner not found")
        return obj
    finally:
        db.close()


@router.put("/owners/{owner_id}", response_model=OwnersRead)
def update_owner(
    owner_id: UUID,
    payload: OwnersUpdate,
    current_user: TokenData = Depends(get_current_user),
):
    db = SessionLocal()
    try:
        require_admin_or_owner(current_user, owner_id)
        obj = db.query(Owners).filter(Owners.id == owner_id).first()
        if not obj:
            raise HTTPException(status_code=404, detail="Owner not found")

        if not is_admin(current_user) and payload.role is not None and payload.role != obj.role:
            raise HTTPException(status_code=403, detail="Role change forbidden for non-admin users")

        data = payload.dict(exclude_unset=True)
        # Exclude updated_at from being set explicitly (let DB handle it)
        data.pop('updated_at', None)
        if not is_admin(current_user):
            data.pop('role', None)
        if 'password' in data and data['password']:
            data['password'] = hash_password(data['password'])
        for k, v in data.items():
            setattr(obj, k, v)
        db.commit()
        db.refresh(obj)
        return obj
    finally:
        db.close()


@router.delete("/owners/{owner_id}")
def delete_owner(owner_id: UUID, current_user: TokenData = Depends(get_current_user)):
    db = SessionLocal()
    try:
        require_admin(current_user)
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
def create_charger(payload: ChargersCreate, current_user: TokenData = Depends(get_current_user)):
    db = SessionLocal()
    try:
        role = get_role(current_user)
        if role == "guest":
            raise HTTPException(status_code=403, detail="Forbidden")
        if is_user_role(current_user):
            if not payload.id_pilot:
                raise HTTPException(status_code=403, detail="Forbidden") # TODO - check
            ensure_pilot_access(db, current_user, payload.id_pilot)

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
def list_chargers(current_user: TokenData = Depends(get_current_user)):
    db = SessionLocal()
    try:
        if is_admin(current_user):
            return db.query(Chargers).all()
        charger_ids = get_accessible_charger_ids(db, current_user, allow_guest=False)
        if not charger_ids:
            return []
        return db.query(Chargers).filter(Chargers.id.in_(charger_ids)).all()
    finally:
        db.close()


@router.get("/chargers/{charger_id}", response_model=ChargersRead)
def get_charger(charger_id: UUID, current_user: TokenData = Depends(get_current_user)):
    db = SessionLocal()
    try:
        return ensure_charger_access(db, current_user, charger_id)
    finally:
        db.close()


@router.put("/chargers/{charger_id}", response_model=ChargersRead)
def update_charger(
    charger_id: UUID,
    payload: ChargersUpdate,
    current_user: TokenData = Depends(get_current_user),
):
    db = SessionLocal()
    try:
        obj = ensure_charger_access(
            db,
            current_user,
            charger_id,
            allow_charger_owner=False,
            allow_pilot_owner=True,
            allow_guest=False,
        )

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
def delete_charger(charger_id: UUID, current_user: TokenData = Depends(get_current_user)):
    db = SessionLocal()
    try:
        if is_admin(current_user):
            obj = db.query(Chargers).filter(Chargers.id == charger_id).first()
            if not obj:
                raise HTTPException(status_code=404, detail="Charger not found")
        else:
            obj = ensure_charger_access(
                db,
                current_user,
                charger_id,
                allow_charger_owner=False,
                allow_pilot_owner=True,
                allow_guest=False,
            )
        db.delete(obj)
        db.commit()
        return {"ok": True}
    finally:
        db.close()


# ---------- Charging Sessions ----------
@router.post("/sessions", response_model=ChargingSessionsRead)
def create_session(payload: ChargingSessionsCreate, current_user: TokenData = Depends(get_current_user)):
    db = SessionLocal()
    try:
        require_admin(current_user)
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
def list_sessions(current_user: TokenData = Depends(get_current_user)):
    db = SessionLocal()
    try:
        if (current_user.role or "").lower() == "admin":
            return db.query(ChargingSessions).all()
        charger_ids = get_accessible_charger_ids(db, current_user, allow_guest=False)
        if not charger_ids:
            return []
        return db.query(ChargingSessions).filter(ChargingSessions.id_charger.in_(charger_ids)).all()
    finally:
        db.close()


@router.get("/sessions/{session_id}", response_model=ChargingSessionsRead)
def get_session(session_id: UUID, current_user: TokenData = Depends(get_current_user)):
    db = SessionLocal()
    try:
        return ensure_session_access(db, current_user, session_id, allow_guest=False)
    finally:
        db.close()


@router.put("/sessions/{session_id}", response_model=ChargingSessionsRead)
def update_session(
    session_id: UUID,
    payload: ChargingSessionsUpdate,
    current_user: TokenData = Depends(get_current_user),
):
    db = SessionLocal()
    try:
        require_admin(current_user)
        obj = db.query(ChargingSessions).filter(ChargingSessions.id == session_id).first()
        if not obj:
            raise HTTPException(status_code=404, detail="Session not found")

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


@router.delete("/sessions/{session_id}")
def delete_session(session_id: UUID, current_user: TokenData = Depends(get_current_user)):
    db = SessionLocal()
    try:
        require_admin(current_user)
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
def create_pilot(payload: PilotCreate, current_user: TokenData = Depends(get_current_user)):
    db = SessionLocal()
    try:
        if not is_admin(current_user):
            require_roles(current_user, "user-adv")
            if payload.id_owner != current_user.owner_id:
                raise HTTPException(status_code=403, detail="Forbidden - you can only create pilots for your own owner ID")
        obj = Pilot(
            id=uuid4(),
            name=payload.name,
            id_owner=payload.id_owner,
            timezone_name=payload.timezone_name,
        )
        db.add(obj)
        db.flush()  # get obj.id before creating the job

        # Auto-create a disabled forecast job for this pilot
        default_job = ForecastJobDB(
            id=uuid4(),
            job_id=f"pilot_{obj.id}_forecast",
            id_pilot=obj.id,
            enabled=False,
            predict_periodicity_minutes=1,
            artifact_path=f"{obj.id}_model.pkl",
            kind="sim",
            freq="15min", #used by reg
            timezone="UTC",
            horizon=None, #used by reg
            sim_steps=16, #used by sim 
            sim_dt_hours=0.25, #used by sim
            sim_power_kw=11.0,
            cal_fraction=0.2,
            lags=None,
        )
        db.add(default_job)
        db.commit()
        db.refresh(obj)
        return obj
    finally:
        db.close()


@router.get("/pilots", response_model=list[PilotRead])
def list_pilots(current_user: TokenData = Depends(get_current_user)):
    db = SessionLocal()
    try:
        require_roles(current_user, "admin", "user", "guest")
        return db.query(Pilot).all()
    finally:
        db.close()


@router.get("/pilots/{pilot_id}", response_model=PilotRead)
def get_pilot(pilot_id: UUID, current_user: TokenData = Depends(get_current_user)):
    db = SessionLocal()
    try:
        require_roles(current_user, "admin", "user", "guest")
        obj = db.query(Pilot).filter(Pilot.id == pilot_id).first()
        if not obj:
            raise HTTPException(status_code=404, detail="Pilot not found")
        return obj
    finally:
        db.close()


@router.put("/pilots/{pilot_id}", response_model=PilotRead)
def update_pilot(
    pilot_id: UUID,
    payload: PilotUpdate,
    current_user: TokenData = Depends(get_current_user),
):
    db = SessionLocal()
    try:
        obj = ensure_pilot_access(
            db,
            current_user,
            pilot_id,
            allow_user=True,
            allow_guest=False,
            require_user_ownership=True,
        )

        data = payload.dict(exclude_unset=True)
        for k, v in data.items():
            setattr(obj, k, v)
        db.commit()
        db.refresh(obj)
        return obj
    finally:
        db.close()


@router.delete("/pilots/{pilot_id}")
def delete_pilot(pilot_id: UUID, current_user: TokenData = Depends(get_current_user)):
    db = SessionLocal()
    try:
        if is_admin(current_user):
            obj = db.query(Pilot).filter(Pilot.id == pilot_id).first()
            if not obj:
                raise HTTPException(status_code=404, detail="Pilot not found")
        else:
            # require_roles(current_user, "user-adv") TODO: check
            obj = ensure_pilot_access(
                db,
                current_user,
                pilot_id,
                allow_user=True,
                allow_guest=False,
                require_user_ownership=True,
            )
        db.delete(obj)
        db.commit()
        return {"ok": True}
    finally:
        db.close()

# ---------- Actions ----------
@router.post("/actions", response_model=ActionsRead)
def create_action(payload: ActionsCreate, current_user: TokenData = Depends(get_current_user)):
    db = SessionLocal()
    try:
        require_admin(current_user)
        obj = Actions(
            id=uuid4(),
            current_time=payload.current_time,
            current_power_kw=payload.current_power_kw,
            energy_delivered_kwh=payload.energy_delivered_kwh,
            is_fully_charged=payload.is_fully_charged,
            probability_disconnection=payload.probability_disconnection,
            cumulative_duration_probability=payload.cumulative_duration_probability,
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
def list_actions(current_user: TokenData = Depends(get_current_user)):
    db = SessionLocal()
    try:
        if is_admin(current_user):
            return db.query(Actions).all()
        charger_ids = get_accessible_charger_ids(db, current_user, allow_guest=False)
        if not charger_ids:
            return []
        return (
            db.query(Actions)
            .join(ChargingSessions, Actions.id_cs == ChargingSessions.id)
            .filter(ChargingSessions.id_charger.in_(charger_ids))
            .all()
        )
    finally:
        db.close()


@router.get("/actions/{action_id}", response_model=ActionsRead)
def get_action(action_id: UUID, current_user: TokenData = Depends(get_current_user)):
    db = SessionLocal()
    try:
        return ensure_action_access(db, current_user, action_id, allow_guest=False)
    finally:
        db.close()


@router.put("/actions/{action_id}", response_model=ActionsRead)
def update_action(
    action_id: UUID,
    payload: ActionsUpdate,
    current_user: TokenData = Depends(get_current_user),
):
    db = SessionLocal()
    try:
        require_admin(current_user)
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
def delete_action(action_id: UUID, current_user: TokenData = Depends(get_current_user)):
    db = SessionLocal()
    try:
        require_admin(current_user)
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
def create_duration(payload: EvDurationCdfCreate, current_user: TokenData = Depends(get_current_user)):
    db = SessionLocal()
    try:
        require_admin(current_user)
        obj = EvDurationCdf(
            id=uuid4(),
            local_hour=payload.local_hour,
            horizon_hours=payload.horizon_hours,
            probability=payload.probability,
            sample_count=payload.sample_count,
            id_charger=payload.id_charger,
        )
        db.add(obj)
        db.commit()
        db.refresh(obj)
        return obj
    finally:
        db.close()


@router.get("/duration_cdf", response_model=list[EvDurationCdfRead])
def list_duration(current_user: TokenData = Depends(get_current_user)):
    db = SessionLocal()
    try:
        require_admin(current_user)
        return db.query(EvDurationCdf).all()
    finally:
        db.close()


@router.get("/duration_cdf/{duration_id}", response_model=EvDurationCdfRead)
def get_duration(duration_id: UUID, current_user: TokenData = Depends(get_current_user)):
    db = SessionLocal()
    try:
        require_admin(current_user)
        obj = db.query(EvDurationCdf).filter(EvDurationCdf.id == duration_id).first()
        if not obj:
            raise HTTPException(status_code=404, detail="Duration CDF not found")
        return obj
    finally:
        db.close()


@router.put("/duration_cdf/{duration_id}", response_model=EvDurationCdfRead)
def update_duration(
    duration_id: UUID,
    payload: EvDurationCdfUpdate,
    current_user: TokenData = Depends(get_current_user),
):
    db = SessionLocal()
    try:
        require_admin(current_user)
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
def delete_duration(duration_id: UUID, current_user: TokenData = Depends(get_current_user)):
    db = SessionLocal()
    try:
        require_admin(current_user)
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
def create_ev_forecast(
    payload: EvForecastStatsCreate,
    current_user: TokenData = Depends(get_current_user),
):
    db = SessionLocal()
    try:
        require_admin(current_user)
        obj = EvForecastStats(
            id=uuid4(),
            local_hour=payload.local_hour,
            mean_energy_kwh=payload.mean_energy_kwh,
            std_energy_kwh=payload.std_energy_kwh,
            mean_duration_hours=payload.mean_duration_hours,
            std_duration_hours=payload.std_duration_hours,
            sample_count=payload.sample_count,
            id_charger=payload.id_charger,
        )
        db.add(obj)
        db.commit()
        db.refresh(obj)
        return obj
    finally:
        db.close()


@router.get("/ev_forecast_stats", response_model=list[EvForecastStatsRead])
def list_ev_forecast(current_user: TokenData = Depends(get_current_user)):
    db = SessionLocal()
    try:
        require_admin(current_user)
        return db.query(EvForecastStats).all()
    finally:
        db.close()


@router.get("/ev_forecast_stats/{forecast_id}", response_model=EvForecastStatsRead)
def get_ev_forecast(forecast_id: UUID, current_user: TokenData = Depends(get_current_user)):
    db = SessionLocal() #TODO - no sense. the user has not access to forecast_id. The user should be able to track the forecast stats by charger id
    try:
        obj = db.query(EvForecastStats).filter(EvForecastStats.id == forecast_id).first()
        if not obj:
            raise HTTPException(status_code=404, detail="EV forecast stat not found")
        if not is_admin(current_user):
            if not obj.id_charger:
                raise HTTPException(status_code=403, detail="Forbidden")
            ensure_charger_access(db, current_user, obj.id_charger, allow_guest=False)
        return obj
    finally:
        db.close()


@router.put("/ev_forecast_stats/{forecast_id}", response_model=EvForecastStatsRead)
def update_ev_forecast(
    forecast_id: UUID,
    payload: EvForecastStatsUpdate,
    current_user: TokenData = Depends(get_current_user),
):
    db = SessionLocal()
    try:
        require_admin(current_user)
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
def delete_ev_forecast(forecast_id: UUID, current_user: TokenData = Depends(get_current_user)):
    db = SessionLocal()
    try:
        require_admin(current_user)
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
def create_grid_load_forecasted(
    payload: GridLoadForecastedCreate,
    current_user: TokenData = Depends(get_current_user),
):
    db = SessionLocal()
    try:
        require_admin(current_user)
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
def list_grid_load_forecasted(current_user: TokenData = Depends(get_current_user)):
    db = SessionLocal()
    try:
        if is_admin(current_user):
            return db.query(GridLoadForecasted).all()

        require_roles(current_user, "user")
        charger_ids = get_accessible_charger_ids(
            db,
            current_user,
            allow_charger_owner=False,
            allow_pilot_owner=True,
            allow_guest=False,
        )
        if not charger_ids:
            return []
        return (
            db.query(GridLoadForecasted)
            .join(Actions, GridLoadForecasted.id_action == Actions.id)
            .join(ChargingSessions, Actions.id_cs == ChargingSessions.id)
            .filter(ChargingSessions.id_charger.in_(charger_ids))
            .all()
        )
    finally:
        db.close()


@router.get("/grid_load_forecasted/{grid_load_id}", response_model=GridLoadForecastedRead)
def get_grid_load_forecasted(
    grid_load_id: UUID,
    current_user: TokenData = Depends(get_current_user),
):
    db = SessionLocal()
    try:
        obj = db.query(GridLoadForecasted).filter(GridLoadForecasted.id == grid_load_id).first()
        if not obj:
            raise HTTPException(status_code=404, detail="Grid load forecasted not found")

        if not is_admin(current_user):
            require_roles(current_user, "user")
            if not obj.id_action:
                raise HTTPException(status_code=403, detail="Forbidden")
            action = db.query(Actions).filter(Actions.id == obj.id_action).first() #required to verify accessibility
            if not action or not action.id_cs:
                raise HTTPException(status_code=403, detail="Forbidden")
            session = ensure_session_access(
                db,
                current_user,
                action.id_cs,
                allow_charger_owner=False,
                allow_pilot_owner=True,
                allow_guest=False,
            )
            if not session:
                raise HTTPException(status_code=403, detail="Forbidden")
        return obj
    finally:
        db.close()


@router.put("/grid_load_forecasted/{grid_load_id}", response_model=GridLoadForecastedRead)
def update_grid_load_forecasted(
    grid_load_id: UUID,
    payload: GridLoadForecastedUpdate,
    current_user: TokenData = Depends(get_current_user),
):
    db = SessionLocal()
    try:
        require_admin(current_user)
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
def delete_grid_load_forecasted(
    grid_load_id: UUID,
    current_user: TokenData = Depends(get_current_user),
):
    db = SessionLocal()
    try:
        require_admin(current_user)
        obj = db.query(GridLoadForecasted).filter(GridLoadForecasted.id == grid_load_id).first()
        if not obj:
            raise HTTPException(status_code=404, detail="Grid load forecasted not found")
        db.delete(obj)
        db.commit()
        return {"ok": True}
    finally:
        db.close()


# ---------- Forecast Jobs ----------
@router.post("/forecast_jobs", response_model=ForecastJobRead)
def create_forecast_job(payload: ForecastJobCreate, current_user: TokenData = Depends(get_current_user)):
    db = SessionLocal()
    try:
        require_admin(current_user)
        existing = db.query(ForecastJobDB).filter(ForecastJobDB.job_id == payload.job_id).first()
        if existing:
            raise HTTPException(status_code=400, detail="job_id already exists")
        pilot = db.query(Pilot).filter(Pilot.id == payload.id_pilot).first()
        if not pilot:
            raise HTTPException(status_code=404, detail="Pilot not found")
        obj = ForecastJobDB(
            id=uuid4(),
            job_id=payload.job_id,
            id_pilot=payload.id_pilot,
            enabled=payload.enabled,
            predict_periodicity_minutes=payload.predict_periodicity_minutes,
            artifact_path=payload.artifact_path,
            kind=payload.kind,
            freq=payload.freq,
            timezone=payload.timezone,
            horizon=payload.horizon,
            sim_steps=payload.sim_steps,
            sim_dt_hours=payload.sim_dt_hours,
            sim_power_kw=payload.sim_power_kw,
            cal_fraction=payload.cal_fraction,
            lags=payload.lags,
        )
        db.add(obj)
        db.commit()
        db.refresh(obj)
        return obj
    finally:
        db.close()


@router.get("/forecast_jobs", response_model=list[ForecastJobRead])
def list_forecast_jobs(current_user: TokenData = Depends(get_current_user)):
    db = SessionLocal()
    try:
        #require_admin(current_user)
        return db.query(ForecastJobDB).all()
    finally:
        db.close()


@router.get("/forecast_jobs/{job_id}", response_model=ForecastJobRead)
def get_forecast_job(job_id: str, current_user: TokenData = Depends(get_current_user)):
    db = SessionLocal()
    try:
        #require_admin(current_user)
        obj = db.query(ForecastJobDB).filter(ForecastJobDB.job_id == job_id).first()
        if not obj:
            raise HTTPException(status_code=404, detail="Forecast job not found")
        return obj
    finally:
        db.close()


@router.put("/forecast_jobs/{job_id}", response_model=ForecastJobRead)
def c(
    job_id: str,
    payload: ForecastJobUpdate,
    current_user: TokenData = Depends(get_current_user),
):
    db = SessionLocal()
    try:
        require_admin(current_user)
        obj = db.query(ForecastJobDB).filter(ForecastJobDB.job_id == job_id).first()
        if not obj:
            raise HTTPException(status_code=404, detail="Forecast job not found")
        data = payload.dict(exclude_unset=True)
        data.pop('updated_at', None)
        # If renaming job_id, check uniqueness
        if 'job_id' in data and data['job_id'] != obj.job_id:
            conflict = db.query(ForecastJobDB).filter(ForecastJobDB.job_id == data['job_id']).first()
            if conflict:
                raise HTTPException(status_code=400, detail="job_id already exists")
        for k, v in data.items():
            setattr(obj, k, v)
        db.commit()
        db.refresh(obj)
        return obj
    finally:
        db.close()


@router.delete("/forecast_jobs/{job_id}")
def delete_forecast_job(job_id: str, current_user: TokenData = Depends(get_current_user)):
    db = SessionLocal()
    try:
        require_admin(current_user)
        obj = db.query(ForecastJobDB).filter(ForecastJobDB.job_id == job_id).first()
        if not obj:
            raise HTTPException(status_code=404, detail="Forecast job not found")
        db.delete(obj)
        db.commit()
        return {"ok": True}
    finally:
        db.close()
