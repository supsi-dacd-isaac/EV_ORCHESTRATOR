from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

import numpy as np


@dataclass
class PolicyDecision:
    # Carries both the discrete action and an optional power level so that
    # binary and power-level policies share one return type across the codebase.
    action: str                         # "charge" | "not_charge" | "idle"
    suggested_power_kw: Optional[float] # kW; None for binary-only policies


class BasePolicy(ABC):
    """Contract every control policy must implement."""

    def enrich_context(
        self,
        db: Any,
        *,
        pilot_id: Optional[UUID],
        at_time: datetime,
        context: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], List[str]]:
        """Add policy-specific signals to ``context`` before ``compute_action``.

        Default: no-op. Override in policies that need external inputs
        (e.g. WindMaxPolicy fetches wind_excess_kw).

        Returns ``(updated_context, warnings)``. Warnings are stored in
        ``actions.decision_context`` by the orchestrator; they must not raise.
        ``db`` is typed loosely so concrete policies can use Session without
        forcing every policy module to import SQLAlchemy.
        """
        return context, []

    @abstractmethod
    def compute_action(
        self,
        obs: np.ndarray,
        context: Optional[Dict[str, Any]] = None,
    ) -> PolicyDecision:
        # `context` carries raw (non-normalized) values that some rule-based
        # policies need but the normalized `obs` vector does not expose,
        # e.g. nominal_power_kw, forecasted_energy_kwh, energy_delivered_kwh,
        # is_fully_charged. Policies that don't need it can ignore the parameter.
        ...

    @abstractmethod
    def describe(self) -> Dict[str, Any]:
        """Self-description of the policy, served by GET /policies.

        Identity fields (control_algorithm, control_policy) are NOT part of this
        dict: the slugs live in the policy registry, which adds them when
        assembling the listing. Keeping them out avoids the class and the
        registry key drifting apart.

        Required keys:
          name (str)              human-readable name
          summary (str)           what the policy does, in a sentence or two
          is_binary (bool)        True when the policy only emits charge /
                                  not_charge and never suggests a power level
          power_levels_kw         fixed list of selectable kW values, or None
                                  when the policy does not pick from a set
          power_selection (str)   "binary" | "discrete_levels" | "fraction_of_nominal"
          rules (list[str])       decision rules in plain language, in the order
                                  they are evaluated
          parameters (dict)       tunable constants (rule-based) or model
                                  metadata (learned policies)

        Optional keys:
          limitations (list[str]) known gaps, e.g. inputs not yet wired
          notes (list[str])       clarifications about how to read the entry
          warnings (list[str])    problems detected in the policy configuration
        """
        ...

    @classmethod
    def source_code(cls) -> str:
        """Python source of the policy class, for GET /policies?include_source=true.

        A classmethod so the source can be read without building an instance —
        an ANN policy would otherwise have to load its weights just to be shown.
        """
        try:
            return inspect.getsource(cls)
        except (OSError, TypeError):
            # Source is unavailable when running from a bundle without .py files.
            return ""
