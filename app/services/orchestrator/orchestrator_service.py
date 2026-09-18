from typing import Dict, List, Optional, Sequence
from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import numpy as np
from sqlalchemy.orm import Session

from app.models import Actions, Chargers, GridLoadForecasted

from app.services.common.error_log import log_system_error
from app.services.load_forecast.load_forecaster_service import (
    SiteLoadBundle,
    fetch_site_load_for_features,
)
from app.services.orchestrator.observation_builder import prepare_observation
from app.services.orchestrator.duration_cdf.query import get_cumulative_duration_probability
from app.services.orchestrator.disconnection_probability import get_disconnection_prob
from app.services.orchestrator.correction_filter import apply_correction
from app.services.orchestrator.policy.ann_policy import AnnPolicy
from app.services.orchestrator.policy.base_policy import BasePolicy, PolicyDecision
from app.services.orchestrator.policy.policy_registry import DEFAULT_RULE_BASED_SLUG, get_policy
from app.services.orchestrator.policy.rule_based.default_policy import DefaultRuleBasedPolicy
from app.services.orchestrator.signals.grid_net_power import (
    GridNetPowerStats,
    features_need_grid_net_power,
    fetch_grid_net_power_stats,
)

def _observation_features_for_policy(policy: BasePolicy) -> Sequence[str]:
    """ANN → sidecar list; rule-based → no observation features.

    Rule-based policies read scalars from ``context`` (including
    ``is_fully_charged``), so they do not need an assembled obs vector and
    must not trigger site-load fetches.
    """
    if isinstance(policy, AnnPolicy):
        return policy.observation_features
    return ()


def compute_and_save_action(
        db: Session,
        session_id,
        charger_id: str,
        timestamp: datetime,
        current_power_kw: float,
        forecasted_energy_kwh: float,
        forecasted_duration_hours: float,
        energy_delivered_kwh: float,
        is_fully_charged: bool,
        controlled_charging_points: int,
        time_connection: datetime,
        forecasted_energy_kwh_std: float,
        forecasted_duration_hours_std: float,
) -> Dict:
    """
    Computes the charging action and saves it to the database.

    Site load (demand/generation/net) is fetched only when the assigned policy's
    observation features need it. A demand failure triggers the Default policy
    fallback (no silent 120 kW fill).

    Resilience contract:
    - Always persists an ``actions`` row for this event when called successfully.
    - If the assigned policy fails, tries ``DefaultRuleBasedPolicy``.
    - If that also fails (or observation cannot be built for it), still persists
      a row with ``suggested_action=NULL``, ``policy_error=True``, and a warning.

    Returns an action dict with keys:
    - charger_id (str)
    - charge (bool | None)  — None when no decision could be produced
    - suggested_power_kw (float | None)
    - valid_until (timestamp)
    - warning (str, optional)
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

    # Soft-fail auxiliary signals so a CDF/disconnection lookup cannot kill the
    # whole decision path (session/event must still get an actions row).
    try:
        prob_discon = get_disconnection_prob(charger_id, connected_hours)
    except Exception as exc:
        prob_discon = 0.0
        log_system_error(
            db,
            source="compute_and_save_action.disconnection_prob",
            error=exc,
            charger_id=charger_id,
            session_id=session_id,
        )
    try:
        cum_prob = get_cumulative_duration_probability(charger_id, connected_hours)
    except Exception as exc:
        cum_prob = 0.0
        log_system_error(
            db,
            source="compute_and_save_action.duration_cdf",
            error=exc,
            charger_id=charger_id,
            session_id=session_id,
        )

    charger = db.query(Chargers).filter(Chargers.id == charger_id).first()
    charger_max_kw = charger.nominal_power if charger else float("inf")
    control_algorithm = charger.control_algorithm if charger else None
    control_policy_slug = charger.control_policy if charger else None
    pilot_id = charger.id_pilot if charger else None

    # Shared session inputs. Policy-specific signals (e.g. wind_excess) are
    # added by policy.enrich_context — the orchestrator does not branch on slug.
    policy_context: Dict = {
        "nominal_power_kw": charger_max_kw,
        "energy_delivered_kwh": energy_delivered_kwh,
        "connected_time_hours": connected_hours,
        "forecasted_energy_kwh": forecasted_energy_kwh,
        "forecasted_duration_hours": forecasted_duration_hours,
        "is_fully_charged": bool(is_fully_charged),
    }
    context_warnings: List[str] = []
    policy_error = False
    policy_error_message: Optional[str] = None
    used_fallback = False
    fallback_failed = False
    fallback_error_message: Optional[str] = None
    load_bundle: Optional[SiteLoadBundle] = None
    grid_stats: Optional[GridNetPowerStats] = None

    decision: Optional[PolicyDecision] = None
    was_corrected = False
    raw_decision: Optional[PolicyDecision] = None
    obs: Optional[np.ndarray] = None
    feature_names: List[str] = []

    # ── Primary path: observation + assigned policy ──────────────────────────
    try:
        policy = get_policy(control_algorithm, control_policy_slug)
        feature_names = list(_observation_features_for_policy(policy))

        load_bundle = fetch_site_load_for_features(
            db, pilot_id, ts_utc, feature_names
        )
        context_warnings.extend(load_bundle.warnings)
        if load_bundle.meta:
            policy_context.setdefault("signal_meta", {})["site_load"] = load_bundle.meta

        if features_need_grid_net_power(feature_names):
            # Hard-fail (→ Default policy) when required measured stats are missing.
            grid_stats = fetch_grid_net_power_stats(db, pilot_id, ts_utc)
            policy_context["grid_net_power_max_kw"] = grid_stats.max_kw
            policy_context["grid_net_power_q40_kw"] = grid_stats.q40_kw
            policy_context["grid_net_power_q70_kw"] = grid_stats.q70_kw
            policy_context.setdefault("signal_meta", {})["grid_net_power"] = {
                "source": grid_stats.source,
                "n_points": grid_stats.n_points,
                "month_start": grid_stats.month_start.isoformat(),
                "month_stop": grid_stats.month_stop.isoformat(),
                "aggregate_every": grid_stats.aggregate_every,
                "import_sensors": grid_stats.import_sensors,
                "export_sensors": grid_stats.export_sensors,
                "scale": grid_stats.scale,
                "max_kw": grid_stats.max_kw,
                "q40_kw": grid_stats.q40_kw,
                "q70_kw": grid_stats.q70_kw,
            }

        nominal_for_obs = None if charger_max_kw == float("inf") else charger_max_kw

        if feature_names:
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
                controlled_charging_points=controlled_charging_points,
                probability_disconnection=prob_discon,
                cumulative_duration_probability=cum_prob,
                feature_names=feature_names,
                net_demand_kw=load_bundle.net.values_kw if load_bundle.net else None,
                demand_kw=load_bundle.demand.values_kw if load_bundle.demand else None,
                generation_kw=(
                    load_bundle.generation.values_kw if load_bundle.generation else None
                ),
                nominal_power_kw=nominal_for_obs,
                grid_net_power_max_kw=(
                    grid_stats.max_kw if grid_stats is not None else None
                ),
                grid_net_power_q40_kw=(
                    grid_stats.q40_kw if grid_stats is not None else None
                ),
                grid_net_power_q70_kw=(
                    grid_stats.q70_kw if grid_stats is not None else None
                ),
            )
            print(f'The observation for charger {charger_id} at {ts_local} (local) is: {obs}')
        else:
            # Rule-based: no ANN obs layout; decisions use policy_context only.
            obs = np.array([], dtype=np.float32)
            print(
                f'No observation vector for charger {charger_id} at {ts_local} '
                f'(rule-based policy; is_fully_charged={is_fully_charged})'
            )

        policy_context, enrich_warnings = policy.enrich_context(
            db,
            pilot_id=pilot_id,
            at_time=ts_utc,
            context=policy_context,
        )
        context_warnings.extend(enrich_warnings)
        raw_decision = policy.compute_action(obs, context=policy_context)
        decision, was_corrected = apply_correction(
            decision=raw_decision,
            is_fully_charged=is_fully_charged,
            is_connected=True,
            charger_max_kw=charger_max_kw,
        )
    except Exception as primary_exc:
        # Assigned policy (or observation / demand forecast) failed — try default.
        policy_error = True
        policy_error_message = str(primary_exc)[:2000]
        log_system_error(
            db,
            source="compute_and_save_action.policy",
            error=primary_exc,
            charger_id=charger_id,
            session_id=session_id,
            context={
                "control_algorithm": control_algorithm,
                "control_policy": control_policy_slug,
            },
        )

        # ── Fallback path: default rule-based policy ─────────────────────────
        try:
            # Default policy reads is_fully_charged from context, not from obs.
            fallback_obs = obs if obs is not None else np.array([], dtype=np.float32)
            raw_decision = DefaultRuleBasedPolicy().compute_action(
                fallback_obs, context=policy_context
            )
            decision, was_corrected = apply_correction(
                decision=raw_decision,
                is_fully_charged=is_fully_charged,
                is_connected=True,
                charger_max_kw=charger_max_kw,
            )
            used_fallback = True
        except Exception as fallback_exc:
            # Both policies failed — still persist a row with suggested_action=NULL.
            fallback_failed = True
            fallback_error_message = str(fallback_exc)[:2000]
            log_system_error(
                db,
                source="compute_and_save_action.default_policy",
                error=fallback_exc,
                charger_id=charger_id,
                session_id=session_id,
                context={
                    "control_algorithm": control_algorithm,
                    "control_policy": control_policy_slug,
                    "primary_error": policy_error_message,
                },
            )
            decision = None
            was_corrected = False
            raw_decision = None

    if decision is not None:
        charge: Optional[bool] = decision.action == "charge"
        suggested_action: Optional[str] = decision.action
        suggested_power_kw = decision.suggested_power_kw
        print(
            f'Policy action is charge={charge} '
            f'(raw={getattr(raw_decision, "action", None)}, corrected={was_corrected})'
        )
    else:
        charge = None
        suggested_action = None
        suggested_power_kw = None
        print(
            f'No policy decision for charger {charger_id}: '
            f'primary={policy_error_message!r}, fallback={fallback_error_message!r}'
        )

    # Snapshot of policy-variable inputs for debugging (actions.decision_context).
    # signal_meta (e.g. wind_excess measurement value + timestamp + source) is
    # kept as its own section rather than mixed into the flat scalar "inputs".
    signal_meta = policy_context.pop("signal_meta", None) if isinstance(policy_context, dict) else None
    # float("inf") is not JSON-serializable — replace if the charger was missing.
    decision_context: Dict = {
        "inputs": {
            **policy_context,
            "nominal_power_kw": (
                None if charger_max_kw == float("inf") else charger_max_kw
            ),
        }
    }
    if signal_meta:
        decision_context["signal_meta"] = signal_meta
    if obs is not None:
        # Snapshot of the vector actually passed to the (primary) policy.
        # Rule-based policies use an empty vector; ANN uses the sidecar layout.
        decision_context["observation"] = {
            "features": list(feature_names),
            "values": [float(x) for x in np.asarray(obs, dtype=np.float64).reshape(-1)],
        }
    if context_warnings:
        decision_context["warnings"] = context_warnings
    if policy_error:
        decision_context["policy_error"] = {
            "message": policy_error_message,
            "assigned_control_algorithm": control_algorithm,
            "assigned_control_policy": control_policy_slug,
            "fallback_control_algorithm": "rule_based",
            "fallback_control_policy": DEFAULT_RULE_BASED_SLUG,
            "used_fallback": used_fallback,
            "fallback_failed": fallback_failed,
            "fallback_error": fallback_error_message,
        }

    # Return valid_until in the caller's timezone if one was provided, otherwise
    # use the pilot's local timezone so the response datetime is always meaningful.
    response_tz = timestamp.tzinfo if timestamp.tzinfo is not None else ZoneInfo(pilot_tz_name)
    valid_until = (ts_utc + timedelta(minutes=15)).astimezone(response_tz)

    # -----------------------------
    # Persist action (always — including suggested_action=NULL on total failure)
    action_id = uuid4()

    # Compose a short DB message covering both failure layers when needed.
    persisted_error_message = policy_error_message
    if fallback_failed and fallback_error_message:
        persisted_error_message = (
            f"Assigned policy failed: {policy_error_message}. "
            f"Default policy also failed: {fallback_error_message}"
        )[:2000]

    # Columns store the policy that actually produced the decision (or the last
    # attempted executor). Assigned policy remains in decision_context.policy_error.
    executed_algorithm = control_algorithm
    executed_policy = control_policy_slug
    if used_fallback or fallback_failed:
        executed_algorithm = "rule_based"
        executed_policy = DEFAULT_RULE_BASED_SLUG

    action_row = Actions(
        id=action_id,
        current_time=ts_utc,
        current_power_kw=current_power_kw,
        energy_delivered_kwh=energy_delivered_kwh,
        is_fully_charged=is_fully_charged,
        probability_disconnection=prob_discon,
        cumulative_duration_probability=cum_prob,
        suggested_action=suggested_action,
        id_cs=session_id,
        control_policy=executed_policy,
        control_algorithm=executed_algorithm,
        correction_applied=was_corrected,
        suggested_power_kw=suggested_power_kw,
        decision_context=decision_context,
        policy_error=policy_error,
        policy_error_message=persisted_error_message,
    )

    db.add(action_row)

    # Persist only the net (or demand) series that the policy actually used.
    series_to_store = None
    if load_bundle is not None:
        series_to_store = load_bundle.net or load_bundle.demand
    if series_to_store is not None:
        for value, ts in zip(series_to_store.values_kw, series_to_store.timestamps):
            db.add(
                GridLoadForecasted(
                    id=uuid4(),
                    value=value,
                    forecast_timestamp=ts,
                    id_action=action_id,
                )
            )

    # NOTE: commit happens in endpoint

    result = {
        'charger_id': charger_id,
        "charge": charge,
        "suggested_power_kw": suggested_power_kw,
        "valid_until": valid_until,
    }
    if fallback_failed:
        result["warning"] = (
            f"Assigned policy (control_algorithm={control_algorithm!r}, "
            f"control_policy={control_policy_slug!r}) failed: {policy_error_message}. "
            f"Default policy also failed: {fallback_error_message}. "
            "No charge decision produced (suggested_action=null); "
            "treat this charger as not charging until the next event."
        )
    elif used_fallback:
        result["warning"] = (
            f"Assigned policy (control_algorithm={control_algorithm!r}, "
            f"control_policy={control_policy_slug!r}) failed: {policy_error_message}. "
            "Used the default rule-based policy instead."
        )
    return result


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
