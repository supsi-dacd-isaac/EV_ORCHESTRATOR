"""Measured grid net-power stats from InfluxDB (Phase 4A).

Net power (kW) at each aligned timestep::

    net = sum(import sensors) − sum(export sensors)

Positive net = drawing energy **from** the grid.
Import sensors meter energy taken from the grid; export sensors meter energy
injected to the grid. An empty export list is allowed (net = sum imports).

Configuration (same connection pattern as wind_excess)::

    data_sources["example_influx"] = { type: influxdb, url, org, bucket, token_secret }

    policy_signals["grid_net_power"] = {
      "enabled": true,
      "source": "example_influx",
      "query": {
        "measurement": "active_power",
        "import_sensors": ["example-grid-import-sensor", "..."],
        "export_sensors": [],          # optional
        "scale": 0.001,                # raw units → kW (default W→kW)
        "aggregate_every": "15m"       # Flux aggregateWindow step (default 15m)
      }
    }

Stats are computed over the **calendar month** of the event in the pilot
timezone, from month-start 00:00 local through the event time.

Never join raw mismatched clocks. Influx ``aggregateWindow`` bins first; Python
then floors leftover timestamps onto the same 15-minute (or configured) grid
and averages points that fall in the same box. Only after that do we add
imports and subtract exports.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from uuid import UUID
from zoneinfo import ZoneInfo

import numpy as np
from sqlalchemy.orm import Session

from app.models import Pilot
from app.services.orchestrator.signals.data_sources import (
    resolve_connection,
    resolve_connection_token,
)
from app.services.orchestrator.signals.influx_client import (
    InfluxPoint,
    InfluxQueryError,
    query_sensor_series,
)

logger = logging.getLogger(__name__)

SIGNAL_KEY = "grid_net_power"
DEFAULT_SCALE = 0.001  # W → kW
DEFAULT_AGGREGATE_EVERY = "15m"

FEATURE_MAX = "grid_net_power_max_month"
FEATURE_Q40 = "grid_net_power_q40_month"
FEATURE_Q70 = "grid_net_power_q70_month"
GRID_NET_POWER_FEATURES: Set[str] = {FEATURE_MAX, FEATURE_Q40, FEATURE_Q70}


class GridNetPowerError(RuntimeError):
    """Required grid net-power stats could not be obtained."""


@dataclass
class GridNetPowerStats:
    """Raw measured stats in kW (before ANN normalization)."""

    max_kw: float
    q40_kw: float
    q70_kw: float
    n_points: int
    source: str
    month_start: datetime
    month_stop: datetime
    aggregate_every: str
    import_sensors: List[str] = field(default_factory=list)
    export_sensors: List[str] = field(default_factory=list)
    scale: float = DEFAULT_SCALE


def features_need_grid_net_power(feature_names: Sequence[str]) -> bool:
    return bool(GRID_NET_POWER_FEATURES.intersection(feature_names))


def get_grid_net_power_config(pilot: Optional[Pilot]) -> Optional[Dict[str, Any]]:
    if pilot is None or not isinstance(pilot.policy_signals, dict):
        return None
    cfg = pilot.policy_signals.get(SIGNAL_KEY)
    if not isinstance(cfg, dict):
        return None
    if cfg.get("enabled") is False:
        return None
    return cfg


def _month_window_utc(at_time: datetime, timezone_name: str) -> Tuple[datetime, datetime]:
    """Calendar month [start, stop) in pilot TZ, returned as UTC-aware datetimes."""
    if at_time.tzinfo is None:
        at_time = at_time.replace(tzinfo=timezone.utc)
    try:
        tz = ZoneInfo(timezone_name or "UTC")
    except Exception:
        tz = ZoneInfo("UTC")
    local = at_time.astimezone(tz)
    start_local = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start_local.astimezone(timezone.utc), at_time.astimezone(timezone.utc)


_DURATION_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def aggregate_every_to_seconds(every: str) -> int:
    """Parse a Flux duration like ``15m`` into seconds."""
    match = re.fullmatch(r"(\d+)([smhdw])", every.strip())
    if not match:
        raise GridNetPowerError(
            f"invalid aggregate_every {every!r}; expected e.g. '15m', '1h'"
        )
    n, unit = match.groups()
    seconds = int(n) * _DURATION_SECONDS[unit]
    if seconds <= 0:
        raise GridNetPowerError(f"aggregate_every must be > 0, got {every!r}")
    return seconds


def floor_to_grid(ts: datetime, every_seconds: int) -> datetime:
    """Snap a timestamp down to the start of its 15-minute (or configured) box."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    utc = ts.astimezone(timezone.utc).replace(microsecond=0)
    epoch = int(utc.timestamp())
    floored = epoch - (epoch % every_seconds)
    return datetime.fromtimestamp(floored, tz=timezone.utc)


def bin_to_grid(
    series: Dict[datetime, float],
    every_seconds: int,
) -> Dict[datetime, float]:
    """Put one sensor onto a regular grid; mean if several points share a box.

    Example with 15-minute boxes: 10:05 and 10:00 both land in the 10:00 box.
    """
    buckets: Dict[datetime, List[float]] = defaultdict(list)
    for ts, value in series.items():
        buckets[floor_to_grid(ts, every_seconds)].append(float(value))
    return {t: sum(vals) / len(vals) for t, vals in buckets.items()}


def _points_to_map(points: List[InfluxPoint], scale: float) -> Dict[datetime, float]:
    out: Dict[datetime, float] = {}
    for p in points:
        if p.time is None:
            continue
        key = p.time.astimezone(timezone.utc).replace(microsecond=0)
        out[key] = float(p.value) * scale
    return out


def build_net_series_kw(
    import_maps: List[Dict[datetime, float]],
    export_maps: List[Dict[datetime, float]],
    *,
    every_seconds: int = 15 * 60,
) -> List[float]:
    """Align sensors onto a common grid, then net = sum(import) − sum(export).

    Missing sensor in a box counts as 0. Do not call this on raw mismatched
    clocks without binning — that is what ``bin_to_grid`` is for.
    """
    import_binned = [bin_to_grid(m, every_seconds) for m in import_maps]
    export_binned = [bin_to_grid(m, every_seconds) for m in export_maps]

    times: Set[datetime] = set()
    for m in import_binned:
        times.update(m)
    for m in export_binned:
        times.update(m)
    if not times:
        return []
    net: List[float] = []
    for t in sorted(times):
        imp = sum(m.get(t, 0.0) for m in import_binned)
        exp = sum(m.get(t, 0.0) for m in export_binned)
        net.append(imp - exp)
    return net


def compute_stats_kw(net_kw: Sequence[float]) -> Tuple[float, float, float]:
    """Return (max, q40, q70) for a net-power series in kW."""
    if not net_kw:
        raise GridNetPowerError("grid net-power series is empty; cannot compute stats")
    arr = np.asarray(net_kw, dtype=np.float64)
    return (
        float(np.max(arr)),
        float(np.quantile(arr, 0.40)),
        float(np.quantile(arr, 0.70)),
    )


def _sensor_list(query: Dict[str, Any], key: str) -> List[str]:
    raw = query.get(key) or []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raise GridNetPowerError(f"query.{key} must be a list of sensor_id strings")
    out = [str(s) for s in raw if s is not None and str(s).strip()]
    return out


def fetch_grid_net_power_stats(
    db: Session,
    pilot_id: Optional[UUID],
    at_time: datetime,
) -> GridNetPowerStats:
    """Fetch monthly grid net-power stats. Raises GridNetPowerError on failure."""
    if pilot_id is None:
        raise GridNetPowerError("no pilot_id; cannot fetch grid_net_power")

    pilot = db.query(Pilot).filter(Pilot.id == pilot_id).first()
    config = get_grid_net_power_config(pilot)
    if config is None:
        raise GridNetPowerError(
            "grid_net_power is not configured for this pilot but the policy "
            "observation requires grid_net_power_*_month features"
        )

    source = config.get("source")
    if not source:
        raise GridNetPowerError("grid_net_power config missing 'source'")

    connection = resolve_connection(pilot, source)
    if connection is None:
        raise GridNetPowerError(f"data_sources has no connection named {source!r}")
    if connection.get("type") != "influxdb":
        raise GridNetPowerError(
            f"connection {source!r} must have type 'influxdb' for grid_net_power"
        )

    try:
        token = resolve_connection_token(db, pilot, connection)
    except Exception as exc:
        raise GridNetPowerError(str(exc)) from exc
    if not token:
        raise GridNetPowerError(
            f"connection {source!r} has no usable token "
            "(missing token_secret or secret not set)"
        )

    query = config.get("query") or {}
    if not isinstance(query, dict):
        raise GridNetPowerError("grid_net_power.query must be a JSON object")

    measurement = query.get("measurement")
    if not measurement:
        raise GridNetPowerError("grid_net_power query requires 'measurement'")

    import_sensors = _sensor_list(query, "import_sensors")
    export_sensors = _sensor_list(query, "export_sensors")
    if not import_sensors:
        raise GridNetPowerError("grid_net_power query.import_sensors must be non-empty")

    scale = float(query.get("scale", DEFAULT_SCALE))
    aggregate_every = str(query.get("aggregate_every") or DEFAULT_AGGREGATE_EVERY)
    tz_name = getattr(pilot, "timezone_name", None) or "UTC"
    start_utc, stop_utc = _month_window_utc(at_time, tz_name)

    if stop_utc <= start_utc:
        raise GridNetPowerError("invalid month window: stop <= start")

    common = dict(
        url=connection.get("url"),
        org=connection.get("org"),
        bucket=connection.get("bucket"),
        token=token,
        measurement=measurement,
        start=start_utc,
        stop=stop_utc,
        aggregate_every=aggregate_every,
    )

    try:
        import_maps = [
            _points_to_map(
                query_sensor_series(sensor_id=sid, **common),
                scale,
            )
            for sid in import_sensors
        ]
        export_maps = [
            _points_to_map(
                query_sensor_series(sensor_id=sid, **common),
                scale,
            )
            for sid in export_sensors
        ]
    except InfluxQueryError as exc:
        raise GridNetPowerError(str(exc)) from exc

    net_kw = build_net_series_kw(
        import_maps,
        export_maps,
        every_seconds=aggregate_every_to_seconds(aggregate_every),
    )
    max_kw, q40_kw, q70_kw = compute_stats_kw(net_kw)

    return GridNetPowerStats(
        max_kw=max_kw,
        q40_kw=q40_kw,
        q70_kw=q70_kw,
        n_points=len(net_kw),
        source=source,
        month_start=start_utc,
        month_stop=stop_utc,
        aggregate_every=aggregate_every,
        import_sensors=import_sensors,
        export_sensors=export_sensors,
        scale=scale,
    )
