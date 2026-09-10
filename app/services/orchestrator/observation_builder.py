"""Build and assemble observation vectors from the named feature catalog.

Phase 1: ``prepare_observation`` still returns the default full vector (same
order/size as before). Internally it builds a feature bank then assembles by
``DEFAULT_OBSERVATION_FEATURES``. Phase 2 will assemble subsets for ANN sidecars.
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, Mapping, Sequence, Union

import numpy as np

from app.services.orchestrator.constants import (
    ENERGY_NEED_95Q,
    DURATION_95Q,
    CHARGING_RATE_CONSTANT,
    NUMBER_CHARGING_POINTS_CONSTANT,
)
from app.services.orchestrator.obs_layout import (
    DEFAULT_FEATURE_START,
    DEFAULT_OBSERVATION_FEATURES,
    FEATURES,
    OBSERVATION_SIZE,
    validate_feature_names,
)


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
    community_load_kw,
    controlled_charging_points: int,
    probability_disconnection: float,
    cumulative_duration_probability: float,
) -> Dict[str, np.ndarray]:
    """Compute every catalog feature as a 1-D float array.

    ``charger_id`` is accepted for API symmetry with callers; features today do
    not branch on it. Returns a dict keyed by feature name (see ``obs_layout``).
    """
    _ = charger_id  # reserved for charger-specific features later

    time_connection_hour = time_connection.hour
    time_current_hour = time_current.hour
    time_current_month = time_current.month
    # Both timestamps are pilot-local (or UTC-aware) after orchestrator normalization.
    connected_time_hours = (time_current - time_connection).total_seconds() / 3600
    max_possible_energy = forecasted_duration_hours * current_power_kw
    community_load = np.asarray(community_load_kw, dtype=np.float64).reshape(-1)
    community_load_kw_mean = float(np.mean(community_load))
    community_load_kw_std = max(float(np.std(community_load)), 1e-6)

    community_load_normalized = np.clip(
        (community_load - community_load_kw_mean) / community_load_kw_std,
        -5,
        5,
    ).astype(np.float32)

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
        "load_level_relative": _f(
            (controlled_charging_points * current_power_kw) / community_load[0]
        ),
        "community_load": community_load_normalized,
    }

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

    The orchestrator still builds the historical full vector; ANN policies use
    this to assemble the subset declared in their sidecar.
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
    community_load_kw,
    controlled_charging_points: int,
    probability_disconnection: float,
    cumulative_duration_probability: float,
    feature_names: Sequence[str] = DEFAULT_OBSERVATION_FEATURES,
) -> np.ndarray:
    """Build an observation vector.

    By default returns the historical full layout (``OBSERVATION_SIZE``).
    Pass ``feature_names`` to assemble a subset / reordering (Phase 2 ANN path).
    """
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
        community_load_kw=community_load_kw,
        controlled_charging_points=controlled_charging_points,
        probability_disconnection=probability_disconnection,
        cumulative_duration_probability=cumulative_duration_probability,
    )
    return assemble_observation(features, feature_names)


def _f(x: Union[float, int, np.floating, np.integer]) -> np.ndarray:
    return np.array([x], dtype=np.float32)


# Re-export so callers can take size from the builder module if preferred.
__all__ = [
    "assemble_observation",
    "build_observation_features",
    "prepare_observation",
    "select_observation_from_default",
    "OBSERVATION_SIZE",
]
