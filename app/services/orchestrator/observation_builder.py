"""Build and assemble observation vectors from the named feature catalog.

``prepare_observation`` returns the default full vector by default. Internally
it builds a feature bank then assembles by ``DEFAULT_OBSERVATION_FEATURES``.
ANN sidecars can pass a subset of names; names that are not in the default
layout (e.g. ``demand_forecast`` alone) require those series to be present in
the feature bank (wired when the forecasters are connected in a later step).
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, Mapping, Optional, Sequence, Union

import numpy as np

from app.services.orchestrator.constants import (
    ENERGY_NEED_95Q,
    DURATION_95Q,
    CHARGING_RATE_CONSTANT,
    NUMBER_CHARGING_POINTS_CONSTANT,
    GRID_NET_POWER_NORM_EPS_KW,
)
from app.services.orchestrator.obs_layout import (
    DEFAULT_FEATURE_START,
    DEFAULT_OBSERVATION_FEATURES,
    FEATURES,
    OBSERVATION_SIZE,
    validate_feature_names,
)


def _normalize_forecast_series(values_kw) -> tuple[np.ndarray, np.ndarray]:
    """Return (raw_kw float64, normalized float32) for a forecast horizon series.

    Shared by net / demand / generation so all three catalog features are scaled
    the same way (z-score, clipped to [-5, 5]).
    """
    raw = np.asarray(values_kw, dtype=np.float64).reshape(-1)
    mean = float(np.mean(raw))
    std = max(float(np.std(raw)), 1e-6)
    normalized = np.clip((raw - mean) / std, -5, 5).astype(np.float32)
    return raw, normalized


def _normalize_grid_net_power_kw(
    raw_kw: float,
    *,
    controlled_charging_points: int,
    current_power_kw: float,
    nominal_power_kw: Optional[float],
) -> float:
    """Divide measured kW by max(n_points × rate, ε); rate prefers current power."""
    rate = float(current_power_kw) if current_power_kw and current_power_kw > 0 else 0.0
    if rate <= 0 and nominal_power_kw is not None and nominal_power_kw > 0:
        rate = float(nominal_power_kw)
    n_points = max(int(controlled_charging_points), 0)
    denom = max(n_points * rate, GRID_NET_POWER_NORM_EPS_KW)
    return float(raw_kw) / denom


def build_observation_features(
    charger_id: str,
    is_fully_charged: bool,
    time_connection: datetime,
    time_current: datetime,
    forecasted_duration_hours: float,
    forecasted_energy_kwh: float,
    current_power_kw: float,
    forecasted_energy_kwh_std: float,
    forecasted_duration_hours_std: float,
    energy_delivered_kwh: float,
    controlled_charging_points: int,
    probability_disconnection: float,
    cumulative_duration_probability: float,
    net_demand_kw: Optional[object] = None,
    demand_kw: Optional[object] = None,
    generation_kw: Optional[object] = None,
    nominal_power_kw: Optional[float] = None,
    grid_net_power_max_kw: Optional[float] = None,
    grid_net_power_q40_kw: Optional[float] = None,
    grid_net_power_q70_kw: Optional[float] = None,
) -> Dict[str, np.ndarray]:
    """Compute catalog features as 1-D float arrays.

    Load series are optional: pass them only when the assembled feature list
    needs ``net_demand_forecast`` / ``demand_forecast`` / ``generation_forecast``
    (or ``load_level_relative``, which uses the first net step).

    Measured grid net-power stats are optional raw kW values; when
    present they are normalized by ``n_charging_points × rate``.
    """
    _ = charger_id  # reserved for charger-specific features later

    time_connection_hour = time_connection.hour
    time_current_hour = time_current.hour
    time_current_month = time_current.month
    connected_time_hours = (time_current - time_connection).total_seconds() / 3600
    max_possible_energy = forecasted_duration_hours * current_power_kw

    features: Dict[str, np.ndarray] = {
        "vehicle_connected": _f(1),
        "fully_charged": _f(int(is_fully_charged)),
        "time_conn_sin": _f(np.sin(2 * np.pi * time_connection_hour / 24)),
        "time_conn_cos": _f(np.cos(2 * np.pi * time_connection_hour / 24)),
        "time_curr_sin": _f(np.sin(2 * np.pi * time_current_hour / 24)),
        "time_curr_cos": _f(np.cos(2 * np.pi * time_current_hour / 24)),
        "month_sin": _f(np.sin(2 * np.pi * (time_current_month - 1) / 12)),
        "month_cos": _f(np.cos(2 * np.pi * (time_current_month - 1) / 12)),
        "connected_time_relative": _f(
            connected_time_hours / (forecasted_duration_hours + 1e-12)
        ),
        "energy_forecasted_rel_capacity": _f(
            forecasted_energy_kwh / (max_possible_energy + 1e-12)
        ),
        "energy_forecasted_log": _f(
            np.log1p(forecasted_energy_kwh) / np.log1p(ENERGY_NEED_95Q)
        ),
        "energy_forecasted_std_norm": _f(
            forecasted_energy_kwh_std / (forecasted_energy_kwh + 1e-12)
        ),
        "duration_forecasted_log": _f(
            np.log1p(forecasted_duration_hours) / np.log1p(DURATION_95Q)
        ),
        "duration_forecasted_std_norm": _f(
            forecasted_duration_hours_std / (forecasted_duration_hours + 1e-12)
        ),
        "probability_disconnection": _f(probability_disconnection),
        "cumulative_duration_probability": _f(cumulative_duration_probability),
        "energy_charged_rel_needed": _f(
            energy_delivered_kwh / forecasted_energy_kwh
        ),
        "charging_rate_norm": _f(current_power_kw / CHARGING_RATE_CONSTANT),
        "num_charging_points_norm": _f(
            controlled_charging_points / NUMBER_CHARGING_POINTS_CONSTANT
        ),
    }

    if net_demand_kw is not None:
        net_raw, net_normalized = _normalize_forecast_series(net_demand_kw)
        features["load_level_relative"] = _f(
            (controlled_charging_points * current_power_kw) / net_raw[0]
        )
        features["net_demand_forecast"] = net_normalized

    if demand_kw is not None:
        _, demand_normalized = _normalize_forecast_series(demand_kw)
        features["demand_forecast"] = demand_normalized
    if generation_kw is not None:
        _, gen_normalized = _normalize_forecast_series(generation_kw)
        features["generation_forecast"] = gen_normalized

    if grid_net_power_max_kw is not None:
        features["grid_net_power_max_month"] = _f(
            _normalize_grid_net_power_kw(
                grid_net_power_max_kw,
                controlled_charging_points=controlled_charging_points,
                current_power_kw=current_power_kw,
                nominal_power_kw=nominal_power_kw,
            )
        )
    if grid_net_power_q40_kw is not None:
        features["grid_net_power_q40_month"] = _f(
            _normalize_grid_net_power_kw(
                grid_net_power_q40_kw,
                controlled_charging_points=controlled_charging_points,
                current_power_kw=current_power_kw,
                nominal_power_kw=nominal_power_kw,
            )
        )
    if grid_net_power_q70_kw is not None:
        features["grid_net_power_q70_month"] = _f(
            _normalize_grid_net_power_kw(
                grid_net_power_q70_kw,
                controlled_charging_points=controlled_charging_points,
                current_power_kw=current_power_kw,
                nominal_power_kw=nominal_power_kw,
            )
        )

    for name, values in features.items():
        expected = FEATURES[name].size
        if values.shape != (expected,):
            raise ValueError(
                f"Feature {name!r} has shape {values.shape}, expected ({expected},)"
            )

    return features


def assemble_observation(
    features: Mapping[str, np.ndarray],
    feature_names: Sequence[str],
) -> np.ndarray:
    """Concatenate named features in order into a 1-D observation vector."""
    problems = validate_feature_names(feature_names)
    if problems:
        raise ValueError("; ".join(problems))

    parts = []
    for name in feature_names:
        if name not in features:
            raise KeyError(f"Feature bank is missing {name!r}")
        values = np.asarray(features[name], dtype=np.float32).reshape(-1)
        expected = FEATURES[name].size
        if values.shape != (expected,):
            raise ValueError(
                f"Feature {name!r} has shape {values.shape}, expected ({expected},)"
            )
        parts.append(values)

    obs = np.concatenate(parts)
    assert obs.ndim == 1, f"Observation must be 1D, got shape {obs.shape}"
    return obs


def select_observation_from_default(
    full_obs: np.ndarray,
    feature_names: Sequence[str],
) -> np.ndarray:
    """Select and reorder slices from a default-layout full observation vector.

    Only features present in ``DEFAULT_OBSERVATION_FEATURES`` can be selected
    this way (e.g. ``net_demand_forecast``). Extra catalog features such as
    ``demand_forecast`` must be assembled from the feature bank instead.
    """
    obs = np.asarray(full_obs, dtype=np.float32).reshape(-1)
    if obs.shape != (OBSERVATION_SIZE,):
        raise ValueError(
            f"Default observation has shape {obs.shape}, expected ({OBSERVATION_SIZE},)"
        )

    problems = validate_feature_names(feature_names)
    if problems:
        raise ValueError("; ".join(problems))

    parts = []
    for name in feature_names:
        if name not in DEFAULT_FEATURE_START:
            raise KeyError(
                f"Feature {name!r} is not in the default observation layout; "
                f"assemble it from the feature bank instead of slicing the default vector."
            )
        start = DEFAULT_FEATURE_START[name]
        size = FEATURES[name].size
        parts.append(obs[start : start + size])
    return np.concatenate(parts)


def prepare_observation(
    charger_id: str,
    is_fully_charged: bool,
    time_connection: datetime,
    time_current: datetime,
    forecasted_duration_hours: float,
    forecasted_energy_kwh: float,
    current_power_kw: float,
    forecasted_energy_kwh_std: float,
    forecasted_duration_hours_std: float,
    energy_delivered_kwh: float,
    controlled_charging_points: int,
    probability_disconnection: float,
    cumulative_duration_probability: float,
    feature_names: Sequence[str] = DEFAULT_OBSERVATION_FEATURES,
    net_demand_kw: Optional[object] = None,
    demand_kw: Optional[object] = None,
    generation_kw: Optional[object] = None,
    nominal_power_kw: Optional[float] = None,
    grid_net_power_max_kw: Optional[float] = None,
    grid_net_power_q40_kw: Optional[float] = None,
    grid_net_power_q70_kw: Optional[float] = None,
) -> np.ndarray:
    """Build an observation vector (default = historical 44-dim layout)."""
    features = build_observation_features(
        charger_id=charger_id,
        is_fully_charged=is_fully_charged,
        time_connection=time_connection,
        time_current=time_current,
        forecasted_duration_hours=forecasted_duration_hours,
        forecasted_energy_kwh=forecasted_energy_kwh,
        current_power_kw=current_power_kw,
        forecasted_energy_kwh_std=forecasted_energy_kwh_std,
        forecasted_duration_hours_std=forecasted_duration_hours_std,
        energy_delivered_kwh=energy_delivered_kwh,
        controlled_charging_points=controlled_charging_points,
        probability_disconnection=probability_disconnection,
        cumulative_duration_probability=cumulative_duration_probability,
        net_demand_kw=net_demand_kw,
        demand_kw=demand_kw,
        generation_kw=generation_kw,
        nominal_power_kw=nominal_power_kw,
        grid_net_power_max_kw=grid_net_power_max_kw,
        grid_net_power_q40_kw=grid_net_power_q40_kw,
        grid_net_power_q70_kw=grid_net_power_q70_kw,
    )
    return assemble_observation(features, feature_names)


def _f(x: Union[float, int, np.floating, np.integer]) -> np.ndarray:
    return np.array([x], dtype=np.float32)


__all__ = [
    "assemble_observation",
    "build_observation_features",
    "prepare_observation",
    "select_observation_from_default",
    "OBSERVATION_SIZE",
]
