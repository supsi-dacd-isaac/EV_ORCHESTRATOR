from __future__ import annotations

from dataclasses import replace
from typing import Tuple

from app.services.orchestrator.policy.base_policy import PolicyDecision


def apply_correction(
    decision: PolicyDecision,
    is_fully_charged: bool,
    is_connected: bool,
    charger_max_kw: float,
) -> Tuple[PolicyDecision, bool]:
    """Enforce physical feasibility on a raw policy decision.

    Rules are checked in order; the first action-level rule that fires wins.
    Power clamping is always checked independently afterwards.
    Returns (corrected_decision, was_corrected).
    """
    corrected = decision
    was_corrected = False

    # Rule 1: cannot charge a fully-charged vehicle
    if is_fully_charged and corrected.action == "charge":
        corrected = PolicyDecision(action="not_charge", suggested_power_kw=None)
        was_corrected = True

    # Rule 2: cannot charge if the vehicle is not physically connected
    elif not is_connected and corrected.action == "charge":
        corrected = PolicyDecision(action="not_charge", suggested_power_kw=None)
        was_corrected = True

    # Rule 3: clamp suggested power to charger rated maximum (active for power-level policies)
    if (
        corrected.action == "charge"
        and corrected.suggested_power_kw is not None
        and corrected.suggested_power_kw > charger_max_kw
    ):
        corrected = replace(corrected, suggested_power_kw=charger_max_kw)
        was_corrected = True

    return corrected, was_corrected
