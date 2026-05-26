from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, List, Optional, Tuple

import requests

from app.config import BASE_LOAD_FORECAST_API_URL
from app.services.orchestrator.constants import (
    BASELOAD_FORECAST_HORIZON_STEPS,
    FORECAST_STEP_MINUTES,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session
    from uuid import UUID

logger = logging.getLogger(__name__)

# Allow up to 35 s for the API to respond (requirement: at least 30 s).
_API_TIMEOUT_SECONDS = 35


def forecast_load(
    db: Optional["Session"] = None,
    pilot_id: Optional["UUID"] = None,
    event_time: Optional[datetime] = None,
) -> Tuple[List[float], List[datetime]]:
    """Return ``(base_load_kw, forecast_timestamps)``.

    ``event_time`` is the timestamp from the charging event.  It is used as
    ``start_time`` in the external-API payload and as the first element of the
    returned timestamp vector.  When omitted, the current UTC wall-clock time
    is used instead.

    ``forecast_timestamps`` is a list of UTC-aware datetimes, one per forecast
    value, with ``FORECAST_STEP_MINUTES``-minute spacing starting from
    ``start_time``.  Callers should zip it directly with ``base_load_kw`` —
    no step arithmetic is needed outside this function.

    When ``BASE_LOAD_FORECAST_API_URL`` is configured *and* ``db`` + ``pilot_id``
    are provided, calls the external load-forecasting API and extracts
    ``response["forecast"]["forecast"]``.  Only the first
    ``BASELOAD_FORECAST_HORIZON_STEPS`` values are used.

    Falls back to the static constant (120 kW) when:
    - the API URL is not configured, or
    - ``db`` / ``pilot_id`` are not supplied, or
    - the API call fails for any reason (error is logged at WARNING level).
    """
    # Resolve start_time from the event timestamp (truncated to the minute) or now.
    if event_time is not None:
        # Ensure UTC-aware, then strip sub-minute precision.
        if event_time.tzinfo is None:
            event_time = event_time.replace(tzinfo=timezone.utc)
        start_time = event_time.astimezone(timezone.utc).replace(second=0, microsecond=0)
    else:
        start_time = datetime.now(tz=timezone.utc).replace(second=0, microsecond=0)

    horizon = BASELOAD_FORECAST_HORIZON_STEPS
    step = timedelta(minutes=FORECAST_STEP_MINUTES)

    if BASE_LOAD_FORECAST_API_URL and db is not None and pilot_id is not None:
        try:
            values, api_start = _call_external_api(db, pilot_id, start_time, horizon)
            timestamps = [api_start + i * step for i in range(len(values))]
            return values, timestamps
        except Exception as exc:
            logger.warning(
                "External load-forecast API call failed; using static fallback. Reason: %s", exc
            )

    # Static fallback
    values = [120.0] * horizon
    timestamps = [start_time + i * step for i in range(horizon)]
    return values, timestamps


def _call_external_api(
    db: "Session",
    pilot_id: "UUID",
    start_time: datetime,
    horizon: int,
) -> Tuple[List[float], datetime]:
    """Call the external API and return ``(values[:horizon], start_time)``.

    Raises ``ValueError`` or ``requests.RequestException`` on any error so the
    caller can fall back to the static value.
    """
    # Import here to avoid a circular import at module load time.
    from app.models import Pilot

    pilot = db.query(Pilot).filter(Pilot.id == pilot_id).first()
    if pilot is None:
        raise ValueError(f"Pilot {pilot_id} not found in the database.")

    meter: Optional[str] = pilot.forecast_meter
    site: Optional[str] = pilot.forecast_site

    if not meter or not site:
        raise ValueError(
            f"Pilot {pilot_id} is missing forecast_meter or forecast_site; "
            "configure these fields to enable the external load-forecast API."
        )

    payload = {
        "meter": meter,
        "site": site,
        "start_time": start_time.isoformat(),
    }

    response = requests.post(
        BASE_LOAD_FORECAST_API_URL,
        json=payload,
        timeout=_API_TIMEOUT_SECONDS,
    )
    response.raise_for_status()

    data = response.json()

    try:
        values = data["forecast"]["forecast"]
    except (KeyError, TypeError) as exc:
        raise ValueError(
            f"Malformed API response — could not extract forecast.forecast. "
            f"Reason: {exc}. Body: {data!r}"
        ) from exc

    if not isinstance(values, list):
        raise ValueError(
            f"Expected a list under forecast.forecast, got {type(values).__name__}."
        )

    if len(values) < horizon:
        raise ValueError(
            f"API returned only {len(values)} forecast values; "
            f"at least {horizon} are required."
        )

    return values[:horizon], start_time
