from datetime import datetime
import numpy as np

from app.services.orchestrator.constants import ENERGY_NEED_95Q, DURATION_95Q, CHARGING_RATE_CONSTANT, NUMBER_CHARGING_POINTS_CONSTANT

def prepare_observation(
    charger_id: str,
    is_fully_charged: bool,
    time_connection: datetime,
    time_current: datetime,
    forecasted_duration_hours: float,
    forecasted_energy_kwh: float,
    current_power_kw: float,
    forecasted_energy_kwh_std:float,
    forecasted_duration_hours_std:float,
    energy_delivered_kwh: float,
    community_load_kw,
    controlled_charging_points: int,
    probability_disconnection: float,
    cumulative_duration_probability: float,
):
    time_connection_hour = time_connection.hour
    time_current_hour = time_current.hour
    time_current_month = time_current.month
    # Both time_connection and time_current are UTC-aware (normalised by orchestrator_service)
    connected_time_hours = (time_current - time_connection).total_seconds()/3600
    max_possible_energy = forecasted_duration_hours * current_power_kw
    community_load_kw_mean = np.mean(community_load_kw)
    community_load_kw_std = max(np.std(community_load_kw), 1e-6)

    vehicle_connected = np.array([1])
    fully_charged = np.array([int(is_fully_charged)])
    time_conn_sin = [np.sin(2 * np.pi * time_connection_hour / 24)]
    time_conn_cos = [np.cos(2 * np.pi * time_connection_hour / 24)]
    time_curr_sin = [np.sin(2 * np.pi * time_current_hour / 24)]
    time_curr_cos = [np.cos(2 * np.pi * time_current_hour / 24)]
    month_sin = [np.sin(2 * np.pi * (time_current_month - 1) / 12)]
    month_cos = [np.cos(2 * np.pi * (time_current_month - 1) / 12)]
    connected_time_relative = _f(connected_time_hours / (forecasted_duration_hours + 1e-12))
    energy_forecasted_relative_to_charging_capacity = _f(forecasted_energy_kwh / (max_possible_energy + 1e-12))
    energy_forecasted_log = _f(np.log1p(forecasted_energy_kwh) / np.log1p(ENERGY_NEED_95Q))
    energy_forecasted_std_norm = _f(forecasted_energy_kwh_std / (forecasted_energy_kwh + 1e-12))
    duration_forecasted_log = _f(np.log1p(forecasted_duration_hours) / np.log1p(DURATION_95Q))
    duration_forecasted_std_norm = _f(forecasted_duration_hours_std / (forecasted_duration_hours + 1e-12))

    probability_disconnection = _f(probability_disconnection)
    cumulative_duration_probability = _f(cumulative_duration_probability)

    energy_charged_rel_needed = _f(energy_delivered_kwh / forecasted_energy_kwh)
    charging_rate_norm = _f(current_power_kw / CHARGING_RATE_CONSTANT)
    num_charging_points_norm = _f(controlled_charging_points / NUMBER_CHARGING_POINTS_CONSTANT)
    load_level_relative = _f((controlled_charging_points * current_power_kw) / community_load_kw[0])
    community_load_kw_normalized = np.clip((community_load_kw - community_load_kw_mean) / community_load_kw_std,-5,5)

    obs = np.concatenate([
        vehicle_connected,
        fully_charged,
        time_conn_sin,
        time_conn_cos,
        time_curr_sin,
        time_curr_cos,
        month_sin,
        month_cos,
        connected_time_relative,
        energy_forecasted_relative_to_charging_capacity,
        energy_forecasted_log,
        energy_forecasted_std_norm,
        duration_forecasted_log,
        duration_forecasted_std_norm,
        probability_disconnection,
        cumulative_duration_probability,
        energy_charged_rel_needed,
        charging_rate_norm,
        num_charging_points_norm,
        load_level_relative,
        community_load_kw_normalized.flatten(),
    ])

    assert obs.ndim == 1, f"Observation must be 1D, got shape {obs.shape}"

    return obs


def _f(x):
    return np.array([x], dtype=np.float32)