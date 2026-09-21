"""Per-pilot site load forecasters (demand / generation / net).

Config lives on the pilot::

    data_sources["…"] = {
      "type": "rest_api",
      "base_url": "https://…/demand_forecaster",
      "auth_secret": "…",
      "auth": {"type": "header", "name": "X-API-Key"}
    }

    policy_signals["demand_forecaster"] = {
      "enabled": true,
      "source": "…",
      "path": "/forecast/example",
      "body": {"site": "example_site", "meter": "example_meter", "start_time": "{{start_time}}"},
      # meter may also be a list → one call per meter, series summed
      "response_path": "demand_forecast",
      "value_field": "forecast",
      "time_field": "timestamp"
    }

    policy_signals["generation_forecaster"] = { … "enabled": false … }

When generation is disabled/unavailable, net = demand (with a warning).
When demand is required but missing/fails, raises SiteForecastError (caller
falls back to Default policy + system_errors).
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from uuid import UUID

from sqlalchemy.orm import Session

from app.models import Pilot
from app.services.orchestrator.constants import (
    BASELOAD_FORECAST_HORIZON_STEPS,
    FORECAST_STEP_MINUTES,
)
from app.services.orchestrator.signals.data_sources import (
    resolve_connection,
    resolve_connection_token,
)
from app.services.orchestrator.signals.rest_client import RestClientError, post_json

logger = logging.getLogger(__name__)

DEMAND_SIGNAL_KEY = "demand_forecaster"
GENERATION_SIGNAL_KEY = "generation_forecaster"

# Observation features that require each series.
_FEATURES_NEEDING_DEMAND: Set[str] = {"demand_forecast"}
_FEATURES_NEEDING_GENERATION: Set[str] = {"generation_forecast"}
_FEATURES_NEEDING_NET: Set[str] = {"net_demand_forecast", "load_level_relative"}


class SiteForecastError(RuntimeError):
    """Demand (or required) forecast could not be obtained."""


@dataclass
class ForecastSeries:
    values_kw: List[float]
    timestamps: List[datetime]
    source: str
    raw_points_used: int


@dataclass
class SiteLoadBundle:
    """Load series actually fetched for this decision (None = not requested)."""

    demand: Optional[ForecastSeries] = None
    generation: Optional[ForecastSeries] = None
    net: Optional[ForecastSeries] = None
    warnings: List[str] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)


def features_need_demand(feature_names: Sequence[str]) -> bool:
    return bool(_FEATURES_NEEDING_DEMAND.intersection(feature_names))


def features_need_generation(feature_names: Sequence[str]) -> bool:
    return bool(_FEATURES_NEEDING_GENERATION.intersection(feature_names))


def features_need_net(feature_names: Sequence[str]) -> bool:
    return bool(_FEATURES_NEEDING_NET.intersection(feature_names))


def features_need_any_site_load(feature_names: Sequence[str]) -> bool:
    return (
        features_need_demand(feature_names)
        or features_need_generation(feature_names)
        or features_need_net(feature_names)
    )


def _series_snapshot(series: ForecastSeries) -> Dict[str, Any]:
    """JSON-friendly raw curve + metadata for actions.decision_context.signal_meta."""
    return {
        "source": series.source,
        "points_used": series.raw_points_used,
        "values_kw": [float(v) for v in series.values_kw],
        "timestamps": [ts.isoformat() for ts in series.timestamps],
    }


def _get_signal_config(pilot: Optional[Pilot], key: str) -> Optional[Dict[str, Any]]:
    if pilot is None or not isinstance(pilot.policy_signals, dict):
        return None
    cfg = pilot.policy_signals.get(key)
    if not isinstance(cfg, dict):
        return None
    if cfg.get("enabled") is False:
        return None
    return cfg


def floor_to_forecast_step(
    dt: datetime,
    step_minutes: int = FORECAST_STEP_MINUTES,
) -> datetime:
    """Floor ``dt`` to the previous 15-minute boundary in UTC (:00/:15/:30/:45).

    The demand forecaster API only accepts starts on that grid; off-grid times
    can return NaN / PLSRegression errors on their side.
    """
    if step_minutes <= 0:
        raise ValueError("step_minutes must be > 0")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    utc = dt.astimezone(timezone.utc).replace(second=0, microsecond=0)
    floored_minute = (utc.minute // step_minutes) * step_minutes
    return utc.replace(minute=floored_minute)


def _fill_body_template(body: Dict[str, Any], start_time: datetime) -> Dict[str, Any]:
    """Deep-copy body and replace ``{{start_time}}`` string placeholders.

    ``start_time`` is floored to the forecast step grid before formatting.
    """
    start_time = floor_to_forecast_step(start_time)
    start_iso = start_time.strftime("%Y-%m-%dT%H:%M:%S+00:00")

    def _walk(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: _walk(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_walk(v) for v in obj]
        if obj == "{{start_time}}":
            return start_iso
        return obj

    return _walk(copy.deepcopy(body))


def _dig(data: Any, path: str) -> Any:
    cur = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise SiteForecastError(f"response missing path {path!r}")
        cur = cur[part]
    return cur


def _parse_forecast_list(
    data: Any,
    *,
    response_path: str,
    value_field: str,
    time_field: str,
    horizon: int,
) -> Tuple[List[float], List[datetime]]:
    raw = _dig(data, response_path) if response_path else data
    if not isinstance(raw, list) or not raw:
        raise SiteForecastError(f"{response_path!r} is empty or not a list")

    values: List[float] = []
    timestamps: List[datetime] = []
    for row in raw[:horizon]:
        if not isinstance(row, dict):
            raise SiteForecastError(f"expected objects under {response_path!r}")
        try:
            values.append(float(row[value_field]))
        except (KeyError, TypeError, ValueError) as exc:
            raise SiteForecastError(
                f"could not read {value_field!r} from forecast point"
            ) from exc
        ts_raw = row.get(time_field)
        if isinstance(ts_raw, str):
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            timestamps.append(ts)
        else:
            timestamps.append(datetime.now(tz=timezone.utc))

    if len(values) < horizon:
        raise SiteForecastError(
            f"forecast returned {len(values)} points; need at least {horizon}"
        )
    return values, timestamps


def _meter_variants(body: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Expand body into one or more POST bodies (list of meters → one each)."""
    meter = body.get("meter")
    if isinstance(meter, list):
        if not meter:
            raise SiteForecastError("body.meter list is empty")
        out = []
        for m in meter:
            b = copy.deepcopy(body)
            b["meter"] = m
            out.append(b)
        return out
    return [body]


def _sum_series(
    series_list: List[Tuple[List[float], List[datetime]]],
) -> Tuple[List[float], List[datetime]]:
    if not series_list:
        raise SiteForecastError("no forecast series to sum")
    n = len(series_list[0][0])
    summed = [0.0] * n
    for values, _ts in series_list:
        if len(values) != n:
            raise SiteForecastError("meter forecasts have unequal lengths")
        for i, v in enumerate(values):
            summed[i] += v
    return summed, series_list[0][1]


def _call_forecaster(
    db: Session,
    pilot: Pilot,
    config: Dict[str, Any],
    start_time: datetime,
    horizon: int,
) -> ForecastSeries:
    source = config.get("source")
    if not source:
        raise SiteForecastError("forecaster config missing 'source'")

    connection = resolve_connection(pilot, source)
    if connection is None:
        raise SiteForecastError(f"data_sources has no connection named {source!r}")
    if connection.get("type") != "rest_api":
        raise SiteForecastError(
            f"connection {source!r} must have type 'rest_api' for a forecaster"
        )

    token = resolve_connection_token(db, pilot, connection)
    if not token:
        raise SiteForecastError(
            f"connection {source!r} has no usable token "
            "(missing auth_secret or secret not set)"
        )

    path = config.get("path") or ""
    body_template = config.get("body") or {}
    if not isinstance(body_template, dict):
        raise SiteForecastError("forecaster body must be a JSON object")

    filled = _fill_body_template(body_template, start_time)
    response_path = config.get("response_path") or "demand_forecast"
    value_field = config.get("value_field") or "forecast"
    time_field = config.get("time_field") or "timestamp"

    collected: List[Tuple[List[float], List[datetime]]] = []
    for body in _meter_variants(filled):
        data = post_json(
            base_url=connection.get("base_url"),
            path=path,
            body=body,
            token=token,
            auth=connection.get("auth"),
        )
        collected.append(
            _parse_forecast_list(
                data,
                response_path=response_path,
                value_field=value_field,
                time_field=time_field,
                horizon=horizon,
            )
        )

    values, timestamps = _sum_series(collected)
    return ForecastSeries(
        values_kw=values,
        timestamps=timestamps,
        source=source,
        raw_points_used=len(values),
    )


def fetch_site_load_for_features(
    db: Session,
    pilot_id: Optional[UUID],
    event_time: datetime,
    feature_names: Sequence[str],
) -> SiteLoadBundle:
    """Fetch only the series required by ``feature_names``.

    Raises SiteForecastError when demand is required but cannot be obtained.
    """
    bundle = SiteLoadBundle()
    need_demand = features_need_demand(feature_names)
    need_gen = features_need_generation(feature_names)
    need_net = features_need_net(feature_names)

    if not (need_demand or need_gen or need_net):
        return bundle

    if event_time.tzinfo is None:
        event_time = event_time.replace(tzinfo=timezone.utc)
    # Floor to :00/:15/:30/:45 — required by the external demand forecaster API.
    start_time = floor_to_forecast_step(event_time)
    horizon = BASELOAD_FORECAST_HORIZON_STEPS

    pilot = db.query(Pilot).filter(Pilot.id == pilot_id).first() if pilot_id else None
    demand_cfg = _get_signal_config(pilot, DEMAND_SIGNAL_KEY)
    gen_cfg = _get_signal_config(pilot, GENERATION_SIGNAL_KEY)

    must_have_demand = need_demand or need_net
    if must_have_demand:
        if demand_cfg is None:
            raise SiteForecastError(
                "demand_forecaster is not configured for this pilot but the "
                "policy observation requires demand/net load features"
            )
        try:
            bundle.demand = _call_forecaster(db, pilot, demand_cfg, start_time, horizon)
        except (SiteForecastError, RestClientError, ValueError) as exc:
            raise SiteForecastError(str(exc)) from exc
        demand_meta = _series_snapshot(bundle.demand)
        demand_meta["start"] = (
            bundle.demand.timestamps[0].isoformat() if bundle.demand.timestamps else None
        )
        bundle.meta["demand"] = demand_meta

    if need_gen or need_net:
        if gen_cfg is not None:
            try:
                bundle.generation = _call_forecaster(
                    db, pilot, gen_cfg, start_time, horizon
                )
                bundle.meta["generation"] = _series_snapshot(bundle.generation)
            except (SiteForecastError, RestClientError, ValueError) as exc:
                # Generation optional when computing net: fall back to demand-only.
                warn = f"generation_forecaster failed; treating generation as 0: {exc}"
                bundle.warnings.append(warn)
                logger.warning(warn)
        elif need_net and bundle.demand is not None:
            bundle.warnings.append(
                "generation_forecaster not configured/enabled; net_demand = demand"
            )
        elif need_gen:
            raise SiteForecastError(
                "generation_forecaster is not configured but the policy requires "
                "generation_forecast"
            )

    if need_net:
        if bundle.demand is None:
            raise SiteForecastError("internal error: demand missing for net")
        if bundle.generation is not None:
            net_vals = [
                d - g
                for d, g in zip(bundle.demand.values_kw, bundle.generation.values_kw)
            ]
        else:
            net_vals = list(bundle.demand.values_kw)
        bundle.net = ForecastSeries(
            values_kw=net_vals,
            timestamps=list(bundle.demand.timestamps),
            source=bundle.demand.source,
            raw_points_used=len(net_vals),
        )
        net_meta = _series_snapshot(bundle.net)
        net_meta["mode"] = (
            "demand_minus_generation" if bundle.generation else "demand_only"
        )
        bundle.meta["net"] = net_meta

    return bundle
