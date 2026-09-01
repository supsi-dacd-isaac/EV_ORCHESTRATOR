from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np

from app.services.orchestrator.policy.base_policy import BasePolicy, PolicyDecision
from app.services.orchestrator.obs_layout import OBS_FULLY_CHARGED


class DefaultRuleBasedPolicy(BasePolicy):
    """Fallback for chargers with no assigned policy.

    Charges at full power whenever connected and not fully charged.
    Does not specify a power level — the correction filter clamps to charger nominal power.
    """

    def compute_action(
        self,
        obs: np.ndarray,
        context: Optional[Dict[str, Any]] = None,
    ) -> PolicyDecision:
        if bool(obs[OBS_FULLY_CHARGED]):
            return PolicyDecision(action="not_charge", suggested_power_kw=None)
        return PolicyDecision(action="charge", suggested_power_kw=None)

    def describe(self) -> Dict[str, Any]:
        return {
            "name": "Default (charge on arrival)",
            "summary": (
                "System fallback used by any charger with no policy assigned. Charges "
                "uninterrupted at the charger's full power until the vehicle reports "
                "fully charged. It shifts no load and needs no forecast."
            ),
            "is_binary": True,
            "power_levels_kw": None,
            "power_selection": "binary",
            "rules": [
                "If the vehicle reports fully charged -> not_charge.",
                "Otherwise -> charge. No power level is suggested, so the charging "
                "power is whatever the charger delivers up to its nominal power.",
            ],
            "parameters": {},
        }
