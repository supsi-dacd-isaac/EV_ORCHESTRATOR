from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

import numpy as np
from sqlalchemy.orm import Session

from app.services.orchestrator.policy.base_policy import BasePolicy, PolicyDecision
from app.services.orchestrator.obs_layout import OBS_FULLY_CHARGED
from app.services.orchestrator.signals.wind_excess import get_wind_excess_kw

logger = logging.getLogger(__name__)

# TODO: replace with the real thresholds once tuned against pilot data.
_ENERGY_RATIO_THRESHOLD = 0.70          # E_charged < 70% of E_median → reduced power
_REDUCED_POWER_FRACTION = 0.5           # fraction of nominal power for "reduced" charging
_WIND_EXCESS_MIN_KW = 1.0               # below this → treat as no usable wind excess


class WindMaxPolicy(BasePolicy):
    """Template for the wind-aware valve policy (control_policy slug: "wind_max").

    Logic (per vehicle not fully charged):
      1. wind_excess >= nominal_power     → charge at maximum power
      2. wind_excess > min threshold and
         wind_excess < nominal_power      → charge at reduced power (0.5 * nominal)
      3. E_charged < 70% of E_median      → charge at reduced power
      4. Time remaining <= (E_median - E_charged) / P_nominal
                                           → charge at maximum power (catch-up)
      5. Otherwise                        → idle

    ``enrich_context`` loads wind_excess_kw from the pilot's policy_signals /
    external DB (placeholder). ``e_median_kwh`` is still approximated with
    forecasted mean energy until a dedicated median query exists.
    """

    def enrich_context(
        self,
        db: Session,
        *,
        pilot_id: Optional[UUID],
        at_time: datetime,
        context: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], List[str]]:
        warnings: List[str] = []
        wind_excess_kw = get_wind_excess_kw(db, pilot_id, at_time)
        if wind_excess_kw is None:
            wind_excess_kw = 0.0
            warning = (
                f"wind_excess not available for pilot {pilot_id} "
                "(missing policy_signals.wind_excess config or external query "
                "not implemented / failed); using 0.0"
            )
            warnings.append(warning)
            logger.warning(warning)
        context = {**context, "wind_excess_kw": wind_excess_kw}
        return context, warnings

    def compute_action(
        self,
        obs: np.ndarray,
        context: Optional[Dict[str, Any]] = None,
    ) -> PolicyDecision:
        if bool(obs[OBS_FULLY_CHARGED]):
            return PolicyDecision(action="not_charge", suggested_power_kw=None)

        context = context or {}
        nominal_power_kw: float = context.get("nominal_power_kw", 0.0)
        energy_delivered_kwh: float = context.get("energy_delivered_kwh", 0.0)
        connected_hours: float = context.get("connected_time_hours", 0.0)

        # TODO: e_median_kwh should be a true median; using forecasted mean as a placeholder.
        e_median_kwh: float = context.get("forecasted_energy_kwh", 0.0)
        # TODO: avg_duration_hours should reflect the average duration for this connection hour.
        avg_duration_hours: float = context.get("forecasted_duration_hours", 0.0)
        wind_excess_kw: float = context.get("wind_excess_kw", 0.0)

        # Wind rules: match charging power to available excess when there is any.
        if wind_excess_kw >= nominal_power_kw and nominal_power_kw > 0:
            return PolicyDecision(action="charge", suggested_power_kw=nominal_power_kw)

        if wind_excess_kw > _WIND_EXCESS_MIN_KW and wind_excess_kw < nominal_power_kw:
            return PolicyDecision(
                action="charge",
                suggested_power_kw=nominal_power_kw * _REDUCED_POWER_FRACTION,
            )

        if energy_delivered_kwh < _ENERGY_RATIO_THRESHOLD * e_median_kwh:
            return PolicyDecision(
                action="charge",
                suggested_power_kw=nominal_power_kw * _REDUCED_POWER_FRACTION,
            )

        time_remaining_hours = avg_duration_hours - connected_hours
        energy_gap_kwh = e_median_kwh - energy_delivered_kwh
        catch_up_hours_needed = energy_gap_kwh / nominal_power_kw if nominal_power_kw > 0 else float("inf")

        if time_remaining_hours <= catch_up_hours_needed:
            return PolicyDecision(action="charge", suggested_power_kw=nominal_power_kw)

        return PolicyDecision(action="not_charge", suggested_power_kw=None)

    def describe(self) -> Dict[str, Any]:
        # Thresholds are read from the module constants so this text cannot
        # drift away from the logic in compute_action().
        return {
            "name": "Wind valve (maximise wind self-consumption)",
            "summary": (
                "Matches charging power to available wind excess when there is enough "
                "of it, and otherwise holds back unless the vehicle is behind on energy "
                "or is running out of connected time. Suggests a power level, so the "
                "charging power is derated rather than only switched on and off."
            ),
            "is_binary": False,
            "power_levels_kw": None,
            "power_selection": "fraction_of_nominal",
            "rules": [
                "If the vehicle reports fully charged -> not_charge.",
                "Else if wind_excess_kw >= charger nominal power -> charge at nominal power.",
                f"Else if wind_excess_kw > {_WIND_EXCESS_MIN_KW:g} kW and "
                f"wind_excess_kw < nominal power -> charge at "
                f"{_REDUCED_POWER_FRACTION:.0%} of the nominal power.",
                f"Else if the energy already delivered is below "
                f"{_ENERGY_RATIO_THRESHOLD:.0%} of the expected session energy -> "
                f"charge at {_REDUCED_POWER_FRACTION:.0%} of the nominal power.",
                "Else if the remaining connected time is no longer than the time needed "
                "to close the energy gap at nominal power -> charge at nominal power "
                "(catch-up).",
                "Otherwise -> not_charge (wait for wind).",
            ],
            "parameters": {
                "energy_ratio_threshold": _ENERGY_RATIO_THRESHOLD,
                "reduced_power_fraction": _REDUCED_POWER_FRACTION,
                "wind_excess_min_kw": _WIND_EXCESS_MIN_KW,
            },
            "limitations": [
                "wind_excess_kw is loaded in enrich_context from "
                "pilot.policy_signals['wind_excess'] via an external-DB placeholder "
                "(app/services/orchestrator/signals/wind_excess.py).",
                "If the pilot has no wind_excess config, or the external query is "
                "not implemented / fails, wind_excess_kw is treated as 0.0 and a "
                "warning is stored in actions.decision_context.",
                "The expected session energy uses the forecasted mean energy for the "
                "connection hour as a stand-in for the historical median.",
                "Thresholds are placeholders and still have to be tuned against pilot data.",
            ],
        }
