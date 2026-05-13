from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func

from app.db.session import SessionLocal
from app.models import EvForecastStats, EvPilotForecastTimeseries, Pilot, ForecastJobDB
from app.schemas.database import EvForecastStatsRead, ForecastJobRead
from app.services.common.auth import TokenData, get_current_user
from app.services.common.authorization import ensure_charger_access, ensure_pilot_access
from app.services.common.constants import GENERIC_CHARGER_ID
from app.services.common.db_utils import to_response_tz

router = APIRouter(prefix="/forecaster", tags=["forecaster"])

_GENERIC_NOTE = (
    "One or more hours use generic fleet-wide defaults because no charger-specific "
    "data is available for that hour yet. This matches the fallback logic used during forecasting."
)


class ForecastHourEntry(BaseModel):
    hour: int
    source: str  # "specific" | "generic"
    mean_energy_kwh: float
    std_energy_kwh: float
    mean_duration_hours: float
    std_duration_hours: float
    sample_count: int
    updated_at: datetime


class ChargerLatestForecast(BaseModel):
    charger_id: UUID
    charger_name: str
    note: Optional[str] = None
    hours: list[ForecastHourEntry]

class TotalOccupancyForecast(BaseModel):
    pilot_id: UUID
    pilot_name: str
    timestamp: datetime
    occupancy: float

class TotalEnergyForecast(BaseModel):
    pilot_id: UUID
    pilot_name: str
    timestamp: datetime
    energy_kWh: float


def _latest_per_hour(db, charger_id) -> dict[int, EvForecastStats]:
    """Return a dict of {local_hour -> latest EvForecastStats row} for the given charger_id."""
    subq = (
        db.query(
            EvForecastStats.local_hour,
            func.max(EvForecastStats.updated_at).label("max_updated_at"),
        )
        .filter(EvForecastStats.id_charger == charger_id)
        .group_by(EvForecastStats.local_hour)
        .subquery()
    )
    rows = (
        db.query(EvForecastStats)
        .join(
            subq,
            (EvForecastStats.id_charger == charger_id)
            & (EvForecastStats.local_hour == subq.c.local_hour)
            & (EvForecastStats.updated_at == subq.c.max_updated_at),
        )
        .all()
    )
    return {row.local_hour: row for row in rows}


def _resolve_forecast_for_charger(
    db, charger_id, tz: str = "UTC"
) -> tuple[list[ForecastHourEntry], bool]:
    """
    Apply the same per-hour fallback logic as get_ev_forecast() in ev_forecast/ev_single_forecaster.py:
    for each hour, use the latest charger-specific row; fall back to generic if absent.
    Returns (entries, any_generic_used).
    """
    specific_by_hour = _latest_per_hour(db, charger_id)
    generic_by_hour = _latest_per_hour(db, GENERIC_CHARGER_ID)

    all_hours = sorted(set(specific_by_hour) | set(generic_by_hour))
    entries: list[ForecastHourEntry] = []
    any_generic = False

    for hour in all_hours:
        row = specific_by_hour.get(hour) or generic_by_hour.get(hour)
        if row is None:
            continue
        source = "specific" if hour in specific_by_hour else "generic"
        if source == "generic":
            any_generic = True
        entries.append(
            ForecastHourEntry(
                hour=hour,
                source=source,
                mean_energy_kwh=row.mean_energy_kwh,
                std_energy_kwh=row.std_energy_kwh,
                mean_duration_hours=row.mean_duration_hours,
                std_duration_hours=row.std_duration_hours,
                sample_count=row.sample_count,
                updated_at=to_response_tz(row.updated_at, tz),
            )
        )

    return entries, any_generic


@router.get("/charger/{charger_id}/latest", response_model=ChargerLatestForecast)
def get_charger_latest_forecast_info(
    charger_id: UUID,
    current_user: TokenData = Depends(get_current_user),
    tz: str = Query(default=None, description="Timezone for timestamps in the response. Defaults to the charger's pilot timezone."),
):
    """
    Return the effective current EV forecast per hour for a specific charger.
    Uses the same per-hour charger-specific strategy, when not available → generic fallback.
    Each hour entry is tagged with source='specific' or source='generic'.
    """
    db = SessionLocal()
    try:
        charger = ensure_charger_access(
            db,
            current_user,
            charger_id,
            allow_charger_owner=True,
            allow_pilot_owner=True,
            allow_guest=False,
        )
        from app.services.common.db_utils import get_pilot_tz_for_charger
        effective_tz = tz if tz is not None else get_pilot_tz_for_charger(db, str(charger.id))
        entries, any_generic = _resolve_forecast_for_charger(db, charger.id, tz=effective_tz)
        return ChargerLatestForecast(
            charger_id=charger.id,
            charger_name=charger.name,
            note=_GENERIC_NOTE if any_generic else None,
            hours=entries,
        )
    finally:
        db.close()


@router.get("/charger/{charger_id}/history", response_model=list[EvForecastStatsRead])
def get_charger_forecast_history_info(
    charger_id: UUID,
    current_user: TokenData = Depends(get_current_user),
):
    """
    Return all historical EV forecast stat records for a specific charger,
    ordered by hour then chronologically.
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
        return (
            db.query(EvForecastStats)
            .filter(EvForecastStats.id_charger == charger_id)
            .order_by(EvForecastStats.local_hour, EvForecastStats.updated_at)
            .all()
        )
    finally:
        db.close()

def _latest_pilot_timeseries_rows(db, pilot_id: UUID) -> tuple[Pilot | None, list[EvPilotForecastTimeseries]]:
    pilot = db.query(Pilot).filter(Pilot.id == pilot_id).first()
    if pilot is None:
        return None, []
    latest_run = (
        db.query(func.max(EvPilotForecastTimeseries.run_at))
        .filter(EvPilotForecastTimeseries.id_pilot == pilot_id)
        .scalar()
    )
    if latest_run is None:
        return pilot, []
    rows = (
        db.query(EvPilotForecastTimeseries)
        .filter(
            EvPilotForecastTimeseries.id_pilot == pilot_id,
            EvPilotForecastTimeseries.run_at == latest_run,
        )
        .order_by(EvPilotForecastTimeseries.forecast_time)
        .all()
    )
    return pilot, rows


@router.get("/pilot/{pilot_id}/total_occupancy", response_model=list[TotalOccupancyForecast])
def forecast_total_occupancy(
    pilot_id: UUID,
    current_user: TokenData = Depends(get_current_user),
    tz: str = Query(default=None, description="Timezone for timestamps in the response. Defaults to the pilot's timezone."),
):
    """Latest Celery-produced pilot time-series: presence (as occupancy) per forecast timestep."""
    db = SessionLocal()
    try:
        pilot, rows = _latest_pilot_timeseries_rows(db, pilot_id)
        if pilot is None:
            raise HTTPException(status_code=404, detail="Pilot not found")
        effective_tz = tz if tz is not None else pilot.timezone_name
        return [
            TotalOccupancyForecast(
                pilot_id=pilot.id,
                pilot_name=pilot.name,
                timestamp=to_response_tz(r.forecast_time, effective_tz),
                occupancy=r.presence,
            )
            for r in rows
        ]
    finally:
        db.close()


@router.get("/pilot/{pilot_id}/total_energy", response_model=list[TotalEnergyForecast])
def forecast_total_energy(
    pilot_id: UUID,
    current_user: TokenData = Depends(get_current_user),
    tz: str = Query(default=None, description="Timezone for timestamps in the response. Defaults to the pilot's timezone."),
):
    """Latest Celery-produced pilot time-series: energy per forecast timestep."""
    db = SessionLocal()
    try:
        pilot, rows = _latest_pilot_timeseries_rows(db, pilot_id)
        if pilot is None:
            raise HTTPException(status_code=404, detail="Pilot not found")
        effective_tz = tz if tz is not None else pilot.timezone_name
        return [
            TotalEnergyForecast(
                pilot_id=pilot.id,
                pilot_name=pilot.name,
                timestamp=to_response_tz(r.forecast_time, effective_tz),
                energy_kWh=r.energy_kwh,
            )
            for r in rows
        ]
    finally:
        db.close()


_VALID_KINDS = {"reg", "reg_prob", "sim"}


class ForecastJobConfigure(BaseModel):
    enabled: Optional[bool] = None
    kind: Optional[str] = None


@router.patch("/pilot/{pilot_id}/forecast_job", response_model=ForecastJobRead)
def configure_pilot_forecast_job(
    pilot_id: UUID,
    payload: ForecastJobConfigure,
    current_user: TokenData = Depends(get_current_user),
):
    """Enable/disable or change the kind of the forecast job for a pilot.
    Accessible by admin and the pilot owner."""
    if payload.kind is not None and payload.kind not in _VALID_KINDS:
        raise HTTPException(status_code=422, detail=f"kind must be one of: {', '.join(sorted(_VALID_KINDS))}")
    db = SessionLocal()
    try:
        ensure_pilot_access(db, current_user, pilot_id, allow_guest=False, require_user_ownership=True)
        job = db.query(ForecastJobDB).filter(ForecastJobDB.id_pilot == pilot_id).first()
        if not job:
            raise HTTPException(status_code=404, detail="No forecast job found for this pilot")
        if payload.enabled is not None:
            job.enabled = payload.enabled
        if payload.kind is not None:
            job.kind = payload.kind
        db.commit()
        db.refresh(job)
        return job
    finally:
        db.close()