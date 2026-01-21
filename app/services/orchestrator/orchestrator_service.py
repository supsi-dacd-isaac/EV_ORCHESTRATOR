from typing import Dict, List
from datetime import datetime, timedelta

from app.services.orchestrator.observation_builder import prepare_observation
from app.services.orchestrator.constants import OBSERVATION_SIZE
from app.services.orchestrator.policy.policy_loader import get_policy


def compute_action(
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
    Returns the action dict with keys:
    -charger_id (str)
    - charge (bool)
    - valid_until (timestamp)
    """

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
        controlled_charging_points=controlled_charging_points
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

    return {
        'charger_id': charger_id,
        "charge": charge,
        "valid_until": valid_until,
    }

