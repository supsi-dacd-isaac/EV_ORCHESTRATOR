from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func

from app.db.session import SessionLocal
from app.models import Chargers, EvForecastStats
from app.schemas.database import EvForecastStatsRead
from app.services.common.auth import TokenData, get_current_user
from app.services.common.authorization import ensure_charger_access
from app.services.common.constants import GENERIC_CHARGER_ID

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


def _latest_per_hour(db, charger_id) -> dict[int, EvForecastStats]:
    """Return a dict of {hour -> latest EvForecastStats row} for the given charger_id."""
    subq = (
        db.query(
            EvForecastStats.hour,
            func.max(EvForecastStats.updated_at).label("max_updated_at"),
        )
        .filter(EvForecastStats.id_charger == charger_id)
        .group_by(EvForecastStats.hour)
        .subquery()
    )
    rows = (
        db.query(EvForecastStats)
        .join(
            subq,
            (EvForecastStats.id_charger == charger_id)
            & (EvForecastStats.hour == subq.c.hour)
            & (EvForecastStats.updated_at == subq.c.max_updated_at),
        )
        .all()
    )
    return {row.hour: row for row in rows}


def _resolve_forecast_for_charger(
    db, charger_id
) -> tuple[list[ForecastHourEntry], bool]:
    """
    Apply the same per-hour fallback logic as get_ev_forecast() in ev_forecast/query.py:
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
                updated_at=row.updated_at,
            )
        )

    return entries, any_generic


@router.get("/charger/{charger_id}/latest", response_model=ChargerLatestForecast)
def get_charger_latest_forecast_info(
    charger_id: UUID,
    current_user: TokenData = Depends(get_current_user),
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
        entries, any_generic = _resolve_forecast_for_charger(db, charger.id)
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
            .order_by(EvForecastStats.hour, EvForecastStats.updated_at)
            .all()
        )
    finally:
        db.close()
