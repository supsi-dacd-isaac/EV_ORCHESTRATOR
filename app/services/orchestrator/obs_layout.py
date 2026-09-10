"""Observation feature catalog for ANN / policy inputs.

Phase 1 of flexible ANN observations: every feature has a stable string name,
a fixed size, and a short description. Sidecars (Phase 2) will list these names
in training order; the admin catalog endpoint (Phase 3) will expose them.

Index constants ``OBS_*`` remain for code that reads the *default full* vector
(e.g. rule-based policies). They are derived from ``DEFAULT_OBSERVATION_FEATURES``
so they stay correct if the default order changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

from app.services.orchestrator.constants import BASELOAD_FORECAST_HORIZON_STEPS


@dataclass(frozen=True)
class ObservationFeature:
    """One named slice of an observation vector."""

    name: str
    size: int
    description: str


# ---------------------------------------------------------------------------
# Catalog — stable names for sidecars and the admin list
# ---------------------------------------------------------------------------

FEATURES: Dict[str, ObservationFeature] = {
    "vehicle_connected": ObservationFeature(
        name="vehicle_connected",
        size=1,
        description="1 when a vehicle is connected (always 1 during an active session).",
    ),
    "fully_charged": ObservationFeature(
        name="fully_charged",
        size=1,
        description="1 when the vehicle reports fully charged, else 0.",
    ),
    "time_conn_sin": ObservationFeature(
        name="time_conn_sin",
        size=1,
        description="sin(2π · connection_hour / 24) in pilot-local time.",
    ),
    "time_conn_cos": ObservationFeature(
        name="time_conn_cos",
        size=1,
        description="cos(2π · connection_hour / 24) in pilot-local time.",
    ),
    "time_curr_sin": ObservationFeature(
        name="time_curr_sin",
        size=1,
        description="sin(2π · current_hour / 24) in pilot-local time.",
    ),
    "time_curr_cos": ObservationFeature(
        name="time_curr_cos",
        size=1,
        description="cos(2π · current_hour / 24) in pilot-local time.",
    ),
    "month_sin": ObservationFeature(
        name="month_sin",
        size=1,
        description="sin(2π · (month-1) / 12) for the current local timestamp.",
    ),
    "month_cos": ObservationFeature(
        name="month_cos",
        size=1,
        description="cos(2π · (month-1) / 12) for the current local timestamp.",
    ),
    "connected_time_relative": ObservationFeature(
        name="connected_time_relative",
        size=1,
        description="Hours already connected divided by forecasted duration.",
    ),
    "energy_forecasted_rel_capacity": ObservationFeature(
        name="energy_forecasted_rel_capacity",
        size=1,
        description="Forecasted energy need over (forecasted duration × current power).",
    ),
    "energy_forecasted_log": ObservationFeature(
        name="energy_forecasted_log",
        size=1,
        description="log1p(forecasted energy) normalized by the energy 95th-percentile constant.",
    ),
    "energy_forecasted_std_norm": ObservationFeature(
        name="energy_forecasted_std_norm",
        size=1,
        description="Forecasted energy std divided by forecasted energy mean.",
    ),
    "duration_forecasted_log": ObservationFeature(
        name="duration_forecasted_log",
        size=1,
        description="log1p(forecasted duration) normalized by the duration 95th-percentile constant.",
    ),
    "duration_forecasted_std_norm": ObservationFeature(
        name="duration_forecasted_std_norm",
        size=1,
        description="Forecasted duration std divided by forecasted duration mean.",
    ),
    "probability_disconnection": ObservationFeature(
        name="probability_disconnection",
        size=1,
        description="Estimated probability the session disconnects soon.",
    ),
    "cumulative_duration_probability": ObservationFeature(
        name="cumulative_duration_probability",
        size=1,
        description="CDF of session duration evaluated at hours already connected.",
    ),
    "energy_charged_rel_needed": ObservationFeature(
        name="energy_charged_rel_needed",
        size=1,
        description="Energy delivered so far divided by forecasted energy need.",
    ),
    "charging_rate_norm": ObservationFeature(
        name="charging_rate_norm",
        size=1,
        description="Current / measured power normalized by the charging-rate constant.",
    ),
    "num_charging_points_norm": ObservationFeature(
        name="num_charging_points_norm",
        size=1,
        description="Controlled charging points normalized by the points constant.",
    ),
    "load_level_relative": ObservationFeature(
        name="load_level_relative",
        size=1,
        description="(controlled points × current power) / first community-load step.",
    ),
    "community_load": ObservationFeature(
        name="community_load",
        size=BASELOAD_FORECAST_HORIZON_STEPS,
        description=(
            f"Normalized community / base-load forecast vector "
            f"({BASELOAD_FORECAST_HORIZON_STEPS} steps of 15 minutes)."
        ),
    ),
}


# Default layout = today's full observation (20 scalars + community_load block).
# Order must match historical prepare_observation() concatenation.
DEFAULT_OBSERVATION_FEATURES: Tuple[str, ...] = (
    "vehicle_connected",
    "fully_charged",
    "time_conn_sin",
    "time_conn_cos",
    "time_curr_sin",
    "time_curr_cos",
    "month_sin",
    "month_cos",
    "connected_time_relative",
    "energy_forecasted_rel_capacity",
    "energy_forecasted_log",
    "energy_forecasted_std_norm",
    "duration_forecasted_log",
    "duration_forecasted_std_norm",
    "probability_disconnection",
    "cumulative_duration_probability",
    "energy_charged_rel_needed",
    "charging_rate_norm",
    "num_charging_points_norm",
    "load_level_relative",
    "community_load",
)


def feature_size(name: str) -> int:
    try:
        return FEATURES[name].size
    except KeyError as exc:
        raise KeyError(f"Unknown observation feature {name!r}") from exc


def observation_dim(feature_names: Sequence[str]) -> int:
    """Total length of a vector assembled from the given ordered feature names."""
    return sum(feature_size(name) for name in feature_names)


def validate_feature_names(feature_names: Sequence[str]) -> List[str]:
    """Return a list of problems (empty means OK)."""
    problems: List[str] = []
    if not feature_names:
        problems.append("observation_features must be a non-empty list.")
        return problems
    seen = set()
    for name in feature_names:
        if name not in FEATURES:
            problems.append(f"Unknown observation feature {name!r}.")
        elif name in seen:
            problems.append(f"Duplicate observation feature {name!r}.")
        seen.add(name)
    return problems


def list_observation_features() -> List[Dict]:
    """Catalog rows exposed by GET /admin/policies/observation-features."""
    default_set = set(DEFAULT_OBSERVATION_FEATURES)
    return [
        {
            "name": spec.name,
            "size": spec.size,
            "description": spec.description,
            "in_default_layout": spec.name in default_set,
        }
        for spec in FEATURES.values()
    ]


# Derived from the default layout — keep in sync automatically.
OBSERVATION_SIZE: int = observation_dim(DEFAULT_OBSERVATION_FEATURES)


def _default_index_map() -> Dict[str, int]:
    index = 0
    mapping: Dict[str, int] = {}
    for name in DEFAULT_OBSERVATION_FEATURES:
        mapping[name] = index
        index += FEATURES[name].size
    return mapping


_DEFAULT_INDEX = _default_index_map()

# Public start-index map for slicing the default full observation vector.
DEFAULT_FEATURE_START: Dict[str, int] = _DEFAULT_INDEX

# Backward-compatible index constants for the default full vector.
OBS_VEHICLE_CONNECTED = _DEFAULT_INDEX["vehicle_connected"]
OBS_FULLY_CHARGED = _DEFAULT_INDEX["fully_charged"]
OBS_TIME_CONN_SIN = _DEFAULT_INDEX["time_conn_sin"]
OBS_TIME_CONN_COS = _DEFAULT_INDEX["time_conn_cos"]
OBS_TIME_CURR_SIN = _DEFAULT_INDEX["time_curr_sin"]
OBS_TIME_CURR_COS = _DEFAULT_INDEX["time_curr_cos"]
OBS_MONTH_SIN = _DEFAULT_INDEX["month_sin"]
OBS_MONTH_COS = _DEFAULT_INDEX["month_cos"]
OBS_CONNECTED_TIME_RELATIVE = _DEFAULT_INDEX["connected_time_relative"]
OBS_ENERGY_FORECASTED_REL_CAPACITY = _DEFAULT_INDEX["energy_forecasted_rel_capacity"]
OBS_ENERGY_FORECASTED_LOG = _DEFAULT_INDEX["energy_forecasted_log"]
OBS_ENERGY_FORECASTED_STD_NORM = _DEFAULT_INDEX["energy_forecasted_std_norm"]
OBS_DURATION_FORECASTED_LOG = _DEFAULT_INDEX["duration_forecasted_log"]
OBS_DURATION_FORECASTED_STD_NORM = _DEFAULT_INDEX["duration_forecasted_std_norm"]
OBS_PROBABILITY_DISCONNECTION = _DEFAULT_INDEX["probability_disconnection"]
OBS_CUMULATIVE_DURATION_PROBABILITY = _DEFAULT_INDEX["cumulative_duration_probability"]
OBS_ENERGY_CHARGED_REL_NEEDED = _DEFAULT_INDEX["energy_charged_rel_needed"]
OBS_CHARGING_RATE_NORM = _DEFAULT_INDEX["charging_rate_norm"]
OBS_NUM_CHARGING_POINTS_NORM = _DEFAULT_INDEX["num_charging_points_norm"]
OBS_LOAD_LEVEL_RELATIVE = _DEFAULT_INDEX["load_level_relative"]
OBS_COMMUNITY_LOAD_START = _DEFAULT_INDEX["community_load"]
