from typing import Dict, List
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models import Actions, GridLoadForecasted

from app.services.orchestrator.observation_builder import prepare_observation
from app.services.orchestrator.duration_cdf.query import get_cumulative_duration_probability
from app.services.orchestrator.disconnection_probability import get_disconnection_prob
from app.services.orchestrator.constants import OBSERVATION_SIZE
from app.services.orchestrator.policy.policy_loader import get_policy


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
) -> Dict:
    """
    Computes the charging action and saves it to the database.

    Returns the action dict with keys:
    -charger_id (str)
    - charge (bool)
    - valid_until (timestamp)
    """

    # compute disconnection statistics (fresh session -> duration 0)
    connected_hours = (timestamp - time_connection).total_seconds() / 3600
    prob_discon = get_disconnection_prob(charger_id, connected_hours)
    cum_prob = get_cumulative_duration_probability(charger_id, connected_hours)

    obs = prepare_observation(
        charger_id=charger_id,
        is_fully_charged=is_fully_charged,
        time_connection=time_connection,
        time_current=timestamp,
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

    print(f'The observation for charger {charger_id} at {timestamp} is: {obs}')
    assert obs.shape == (OBSERVATION_SIZE,), (
        f"Invalid observation shape {obs.shape}, expected ({OBSERVATION_SIZE},)"
    )
    policy = get_policy()
    charge = bool(policy.compute_action(obs))
    print(f'Policy action is charge={charge}')
    #
    # #Dummy logic
    # if is_fully_charged or energy_delivered_kwh >= forecasted_energy_kwh:
    #     charge = False
    # else:
    #     charge = True

    valid_until = timestamp + timedelta(minutes=15)

    # -----------------------------
    # Persist action
    action_id = uuid4()

    action_row = Actions(
        id=action_id,
        current_time=timestamp,
        current_power_kw=current_power_kw,
        energy_delivered_kwh=energy_delivered_kwh,
        is_fully_charged=is_fully_charged,
        probability_disconnection=prob_discon,
        cumulative_duration_probability=cum_prob,
        action="charge" if charge else "not_charge",
        id_cs=session_id,
    )

    db.add(action_row)

    # Persist load forecast
    # -----------------------------
    for value in community_load_kw:
        forecast_row = GridLoadForecasted(
            id=uuid4(),
            value=value,
            id_action=action_id,
        )
        db.add(forecast_row)

    # NOTE: commit happens in endpoint

    return {
        'charger_id': charger_id,
        "charge": charge,
        "valid_until": valid_until,
    }
