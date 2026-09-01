from typing import Dict, List, Optional
from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.models import Actions, Chargers, GridLoadForecasted

from app.services.orchestrator.observation_builder import prepare_observation
from app.services.orchestrator.duration_cdf.query import get_cumulative_duration_probability
from app.services.orchestrator.disconnection_probability import get_disconnection_prob
from app.services.orchestrator.constants import OBSERVATION_SIZE, FORECAST_STEP_MINUTES
from app.services.orchestrator.correction_filter import apply_correction
from app.services.orchestrator.policy.policy_registry import get_policy


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
    # A naive input timestamp is interpreted as pilot-local wall-clock time.
    from app.services.common.db_utils import to_utc, to_pilot_time, get_pilot_tz_for_charger
    pilot_tz_name = get_pilot_tz_for_charger(db, charger_id)
    ts_utc = to_utc(timestamp, local_tz=pilot_tz_name)
    tc_utc = to_utc(time_connection, local_tz=pilot_tz_name)

    # Fill real_action/real_power_kw on the previous action row before creating the new one.
    _fill_previous_action(db, session_id, energy_delivered_kwh, ts_utc)

    # Convert both timestamps to pilot-local time so that observation features
    # (e.g. hour-of-day) are expressed in local time rather than UTC.
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
    charger = db.query(Chargers).filter(Chargers.id == charger_id).first()
    charger_max_kw = charger.nominal_power if charger else float("inf")
    control_algorithm = charger.control_algorithm if charger else None
    control_policy_slug = charger.control_policy if charger else None
    pilot_id = charger.id_pilot if charger else None

    policy = get_policy(control_algorithm, control_policy_slug)
    # Shared session inputs. Policy-specific signals (e.g. wind_excess) are
    # added by policy.enrich_context — the orchestrator does not branch on slug.
    policy_context: Dict = {
        "nominal_power_kw": charger_max_kw,
        "energy_delivered_kwh": energy_delivered_kwh,
        "connected_time_hours": connected_hours,
        "forecasted_energy_kwh": forecasted_energy_kwh,
        "forecasted_duration_hours": forecasted_duration_hours,
    }
    policy_context, context_warnings = policy.enrich_context(
        db,
        pilot_id=pilot_id,
        at_time=ts_utc,
        context=policy_context,
    )

    raw_decision = policy.compute_action(obs, context=policy_context)
    decision, was_corrected = apply_correction(
        decision=raw_decision,
        is_fully_charged=is_fully_charged,
        is_connected=True,
        charger_max_kw=charger_max_kw,
    )
    charge = decision.action == "charge"
    print(f'Policy action is charge={charge} (raw={raw_decision.action}, corrected={was_corrected})')

    # Snapshot of policy-variable inputs for debugging (actions.decision_context).
    # float("inf") is not JSON-serializable — replace if the charger was missing.
    decision_context: Dict = {
        "inputs": {
            **policy_context,
            "nominal_power_kw": (
                None if charger_max_kw == float("inf") else charger_max_kw
            ),
        }
    }
    if context_warnings:
        decision_context["warnings"] = context_warnings

    # Return valid_until in the caller's timezone if one was provided, otherwise
    # use the pilot's local timezone so the response datetime is always meaningful.
    response_tz = timestamp.tzinfo if timestamp.tzinfo is not None else ZoneInfo(pilot_tz_name)
    valid_until = (ts_utc + timedelta(minutes=15)).astimezone(response_tz)

    # -----------------------------
    # Persist action
    action_id = uuid4()

    action_row = Actions(
        id=action_id,
        current_time=ts_utc,
        current_power_kw=current_power_kw,
        energy_delivered_kwh=energy_delivered_kwh,
        is_fully_charged=is_fully_charged,
        probability_disconnection=prob_discon,
        cumulative_duration_probability=cum_prob,
        suggested_action=decision.action,
        id_cs=session_id,
        control_policy=control_policy_slug,
        control_algorithm=control_algorithm,
        correction_applied=was_corrected,
        suggested_power_kw=decision.suggested_power_kw,
        decision_context=decision_context,
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
        "suggested_power_kw": decision.suggested_power_kw,
        "valid_until": valid_until,
    }


# ── Retrospective real-action helpers ─────────────────────────────────────────

_ENERGY_CHARGE_THRESHOLD_KWH = 0.05  # below this delta → treat as not_charge


def _fill_previous_action(
    db: Session,
    session_id,
    current_energy_kwh: float,
    current_time: datetime,
) -> None:
    """Update the most recent unfilled action row for the session with the real outcome."""
    prev_action = (
        db.query(Actions)
        .filter(Actions.id_cs == session_id, Actions.real_action == None)  # noqa: E711
        .order_by(Actions.current_time.desc())
        .first()
    )

    if prev_action is None:
        return

    delta_energy = current_energy_kwh - prev_action.energy_delivered_kwh
    delta_hours = max(
        (current_time - prev_action.current_time).total_seconds() / 3600.0,
        0.001,
    )
    prev_action.real_action = "charge" if delta_energy > _ENERGY_CHARGE_THRESHOLD_KWH else "not_charge"
    prev_action.real_power_kw = delta_energy / delta_hours if delta_energy > _ENERGY_CHARGE_THRESHOLD_KWH else 0.0


def finalize_session_action(
    db: Session,
    session_id,
    final_energy_kwh: float,
    disconnection_time: datetime,
) -> None:
    """Fill the last action row of a closing session using the session's final energy total."""
    _fill_previous_action(db, session_id, final_energy_kwh, disconnection_time)

