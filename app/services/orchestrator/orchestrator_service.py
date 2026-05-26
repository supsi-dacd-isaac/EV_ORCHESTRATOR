from typing import Dict, List, Optional
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo


from sqlalchemy.orm import Session

from app.models import Actions, GridLoadForecasted

from app.services.orchestrator.observation_builder import prepare_observation
from app.services.orchestrator.duration_cdf.query import get_cumulative_duration_probability
from app.services.orchestrator.disconnection_probability import get_disconnection_prob
from app.services.orchestrator.constants import OBSERVATION_SIZE, FORECAST_STEP_MINUTES

from app.services.orchestrator.policy.policy_loader import get_policy
from app.config import ACTOR_MODEL_PATH


def compute_and_save_action(
        db: Session,
        session_id,
        charger_id: str,
        timestamp: datetime,
        current_power_kw: float,
        forecasted_energy_kwh:float,
        forecasted_duration_hours: float,
        energy_delivered_kwh: float,
        community_load_kw: List[float],
        is_fully_charged: bool,
        controlled_charging_points: int,
        time_connection: datetime,
        forecasted_energy_kwh_std: float,
        forecasted_duration_hours_std: float,
        forecast_timestamps: Optional[List[datetime]] = None,
) -> Dict:
    """
    Computes the charging action and saves it to the database.

    Returns the action dict with keys:
    -charger_id (str)
    - charge (bool)
    - valid_until (timestamp)
    """

    # Normalize both to UTC-aware — DB convention is TIMESTAMP WITH TIME ZONE.
    # All event timestamps from the API may be tz-aware or naive (assumed UTC).
    from app.services.common.db_utils import to_utc, to_pilot_time, get_pilot_tz_for_charger
    ts_utc = to_utc(timestamp)
    tc_utc = to_utc(time_connection)

    # Resolve the pilot's local timezone and convert both timestamps to pilot-local
    # time so that observation features (e.g. hour-of-day) are expressed in local
    # time rather than UTC.
    pilot_tz_name = get_pilot_tz_for_charger(db, charger_id)
    ts_local = to_pilot_time(ts_utc, pilot_tz_name)
    tc_local = to_pilot_time(tc_utc, pilot_tz_name)

    connected_hours = (ts_utc - tc_utc).total_seconds() / 3600
    prob_discon = get_disconnection_prob(charger_id, connected_hours)
    cum_prob = get_cumulative_duration_probability(charger_id, connected_hours)

    obs = prepare_observation(
        charger_id=charger_id,
        is_fully_charged=is_fully_charged,
        time_connection=tc_local,
        time_current=ts_local,
        forecasted_duration_hours=forecasted_duration_hours,
        forecasted_energy_kwh=forecasted_energy_kwh,
        current_power_kw=current_power_kw,
        forecasted_energy_kwh_std=forecasted_energy_kwh_std,
        forecasted_duration_hours_std=forecasted_duration_hours_std,
        energy_delivered_kwh=energy_delivered_kwh,
        community_load_kw=community_load_kw,
        controlled_charging_points=controlled_charging_points,
        probability_disconnection=prob_discon,
        cumulative_duration_probability=cum_prob
    )

    print(f'The observation for charger {charger_id} at {ts_local} (local) is: {obs}')
    assert obs.shape == (OBSERVATION_SIZE,), (
        f"Invalid observation shape {obs.shape}, expected ({OBSERVATION_SIZE},)"
    )
    policy = get_policy()
    charge = bool(policy.compute_action(obs))
    print(f'Policy action is charge={charge}')

    # Return valid_until in the caller's timezone if one was provided, otherwise
    # use the pilot's local timezone so the response datetime is always meaningful.
    response_tz = timestamp.tzinfo if timestamp.tzinfo is not None else ZoneInfo(pilot_tz_name)
    valid_until = (ts_utc + timedelta(minutes=15)).astimezone(response_tz)

    # -----------------------------
    # Persist action
    action_id = uuid4()

    action_row = Actions(
        id=action_id,
        current_time=ts_utc,  # UTC-aware — matches TIMESTAMP WITH TIME ZONE column
        current_power_kw=current_power_kw,
        energy_delivered_kwh=energy_delivered_kwh,
        is_fully_charged=is_fully_charged,
        probability_disconnection=prob_discon,
        cumulative_duration_probability=cum_prob,
        action="charge" if charge else "not_charge",
        id_cs=session_id,
        policy=Path(ACTOR_MODEL_PATH).name,
    )

    db.add(action_row)

    # Persist load forecast
    # -----------------------------
    # Zip values with their pre-computed timestamps.  When no timestamp vector
    # was supplied (e.g. tests), generate one on the spot as a safe fallback.
    if forecast_timestamps is None:
        step = timedelta(minutes=FORECAST_STEP_MINUTES)
        forecast_timestamps = [ts_utc + i * step for i in range(len(community_load_kw))]
    for value, ts in zip(community_load_kw, forecast_timestamps):
        forecast_row = GridLoadForecasted(
            id=uuid4(),
            value=value,
            forecast_timestamp=ts,
            id_action=action_id,
        )
        db.add(forecast_row)

    # NOTE: commit happens in endpoint

    return {
        'charger_id': charger_id,
        "charge": charge,
        "valid_until": valid_until,
    }
