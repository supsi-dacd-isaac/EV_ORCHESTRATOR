"""Wind-excess signal: config on the pilot, value read from InfluxDB.

Configuration lives in two places on the pilot:
  - policy_signals["wind_excess"] : { enabled, source, query{...} }
  - data_sources[<source>]        : the InfluxDB connection (url/org/bucket +
                                     token_secret pointing at an encrypted token)

The value the wind sensor reports is already the NET excess (production minus
consumption) in WATTS; we scale it to kW (default 0.001) for the policy. The
live value + its measurement time are snapshotted into actions.decision_context
by the policy's enrich_context; this module only knows how to fetch it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.models import Pilot
from app.services.common.error_log import log_system_error
from app.services.orchestrator.signals.data_sources import (
    resolve_connection,
    resolve_connection_token,
)
from app.services.orchestrator.signals.influx_client import query_last_value

logger = logging.getLogger(__name__)

SIGNAL_KEY = "wind_excess"
DEFAULT_SCALE = 0.001          # W -> kW
DEFAULT_LOOKBACK_MINUTES = 30


@dataclass
class WindExcessReading:
    value_kw: float
    measured_at: Optional[datetime]
    raw_value_w: Optional[float]
    scale: float
    source: str


def get_wind_excess_config(pilot: Optional[Pilot]) -> Optional[Dict[str, Any]]:
    """Return the ``wind_excess`` block from ``pilot.policy_signals``, or None."""
    if pilot is None or not isinstance(pilot.policy_signals, dict):
        return None
    cfg = pilot.policy_signals.get(SIGNAL_KEY)
    if not isinstance(cfg, dict):
        return None
    if cfg.get("enabled") is False:
        return None
    return cfg


def is_wind_excess_configured(pilot: Optional[Pilot]) -> bool:
    return get_wind_excess_config(pilot) is not None


def _fetch_reading(
    db: Session,
    pilot: Pilot,
    config: Dict[str, Any],
    at_time: datetime,
) -> Optional[WindExcessReading]:
    """Resolve the connection, run the query, and build a reading.

    Raises on misconfiguration / query failure; the public wrapper turns those
    into a logged system error + None so callers can degrade to 0.0.
    """
    source = config.get("source")
    if not source:
        raise ValueError("wind_excess config has no 'source' (data_sources connection name)")

    connection = resolve_connection(pilot, source)
    if connection is None:
        raise ValueError(f"data_sources has no connection named {source!r}")

    conn_type = connection.get("type")
    if conn_type != "influxdb":
        raise ValueError(f"connection {source!r} has unsupported type {conn_type!r} for wind_excess")

    token = resolve_connection_token(db, pilot, connection)
    if not token:
        raise ValueError(
            f"connection {source!r} has no usable token "
            "(missing 'token_secret' or the secret is not set)"
        )

    query = config.get("query") or {}
    measurement = query.get("measurement")
    sensor_id = query.get("sensor_id")
    if not measurement or not sensor_id:
        raise ValueError("wind_excess query requires 'measurement' and 'sensor_id'")

    scale = float(query.get("scale", DEFAULT_SCALE))
    lookback = int(query.get("lookback_minutes", DEFAULT_LOOKBACK_MINUTES))

    point = query_last_value(
        url=connection.get("url"),
        org=connection.get("org"),
        bucket=connection.get("bucket"),
        token=token,
        measurement=measurement,
        sensor_id=sensor_id,
        at_time=at_time,          # query the window ending at the decision time
        lookback_minutes=lookback,
    )
    if point is None:
        # Configured and reachable, but no data in the window — soft condition.
        logger.warning(
            "wind_excess: no data for sensor %r in last %dm (pilot %s)",
            sensor_id, lookback, pilot.id,
        )
        return None

    return WindExcessReading(
        value_kw=point.value * scale,
        measured_at=point.time,
        raw_value_w=point.value,
        scale=scale,
        source=source,
    )


def get_wind_excess_reading(
    db: Session,
    pilot_id: Optional[UUID],
    at_time: datetime,
) -> Optional[WindExcessReading]:
    """Fetch the wind-excess reading for a pilot, or None.

    Returns None when the pilot/signal is not configured (caller warns and uses
    0.0). When the signal IS configured but the query fails, records a
    system_errors row and still returns None — never raises.

    Required pilot configuration (both blocks):

        pilot.data_sources = {
            "example_influx": {
                "type": "influxdb",
                "url": "https://example.com/influxdb/",
                "org": "interped",
                "bucket": "interped",
                "token_secret": "example_influx_token"   # -> pilot_secret row
            }
        }

        pilot.policy_signals = {
            "wind_excess": {
                "enabled": true,
                "source": "example_influx",              # a data_sources key
                "query": {
                    "measurement": "active_power",
                    "sensor_id": "example-wind-sensor",
                    "aggregate": "last",
                    "lookback_minutes": 30,                # optional (default 30)
                    "scale": 0.001                          # optional (default: W->kW)
                }
            }
        }

    The token itself is NOT stored in data_sources; it is set separately via
    PUT /db/pilots/{id}/secrets/example_influx_token and stored encrypted.
    """
    if pilot_id is None:
        return None

    pilot = db.query(Pilot).filter(Pilot.id == pilot_id).first()
    config = get_wind_excess_config(pilot)
    if config is None:
        return None  # not configured for this pilot

    try:
        return _fetch_reading(db, pilot, config, at_time)
    except Exception as exc:  # noqa: BLE001 - signal fetch must never raise into the policy
        log_system_error(
            db,
            source="signals.wind_excess",
            error=exc,
            context={"pilot_id": str(pilot_id), "at_time": at_time.isoformat()},
        )
        return None
