from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from app.services.orchestrator.policy.base_policy import BasePolicy
from app.services.orchestrator.policy.ann_policy import (
    AnnPolicy,
    require_ann_sidecar_config,
)
from app.services.orchestrator.policy.rule_based.default_policy import DefaultRuleBasedPolicy
from app.services.orchestrator.policy.rule_based.wind_max_policy import WindMaxPolicy

logger = logging.getLogger(__name__)

# ── Rule-based registry ─────────────────────────────────────────
# Maps policy slug → class.  Add one entry here when creating a new rule policy.
DEFAULT_RULE_BASED_SLUG = "default"

_RULE_BASED_REGISTRY: dict[str, type[BasePolicy]] = {
    DEFAULT_RULE_BASED_SLUG: DefaultRuleBasedPolicy,
    "wind_max": WindMaxPolicy,
}

# ── In-memory cache ───────────────────────────────────────────────────────────
# Key format: "algorithm:policy_slug", e.g. "ann:policy_20251204_...pth"
_POLICY_CACHE: dict[str, BasePolicy] = {}

_DEFAULT_CACHE_KEY = f"rule_based:{DEFAULT_RULE_BASED_SLUG}"


def models_dir() -> Path:
    """Directory holding the ANN .pth files and their required .json sidecars."""
    from app.config import ACTOR_MODEL_PATH

    return Path(ACTOR_MODEL_PATH).parent


def get_policy(
    control_algorithm: Optional[str],
    control_policy: Optional[str],
) -> BasePolicy:
    """Return a (cached) policy instance for the given algorithm + slug.

    NULL/NULL → DefaultRuleBasedPolicy (system fallback).
    """
    if not control_algorithm or not control_policy:
        return _get_or_load(_DEFAULT_CACHE_KEY, DefaultRuleBasedPolicy)

    cache_key = f"{control_algorithm}:{control_policy}"

    if control_algorithm == "ann":
        return _get_or_load(cache_key, lambda: _load_ann(control_policy))

    if control_algorithm == "rule_based":
        return _get_or_load(cache_key, lambda: _load_rule_based(control_policy))

    raise ValueError(
        f"Unknown control_algorithm '{control_algorithm}'. "
        f"Supported: ann, rule_based."
    )


def invalidate_policy_cache(control_algorithm: str, control_policy: str) -> bool:
    """Drop a cached instance so the next request reloads it. Returns True if found."""
    key = f"{control_algorithm}:{control_policy}"
    if key in _POLICY_CACHE:
        del _POLICY_CACHE[key]
        logger.info("Policy cache invalidated: '%s'", key)
        return True
    return False


def validate_policy_reference(
    control_algorithm: Optional[str],
    control_policy: Optional[str],
) -> None:
    """Raise ValueError if the (algorithm, policy) pair does not exist.

    Used by the charger policy-assignment endpoint to reject invalid input
    before it is saved, without loading the (possibly expensive) policy.
    """
    if not control_algorithm and not control_policy:
        return  # NULL/NULL is always valid — resolves to the system default

    if not control_algorithm or not control_policy:
        raise ValueError("control_algorithm and control_policy must be set together, or both left empty.")

    if control_algorithm == "ann":
        model_path = models_dir() / control_policy
        if not model_path.exists():
            raise ValueError(f"ANN model file not found: '{control_policy}'.")
        # Ensure the sidecar declares a usable observation layout before assignment.
        require_ann_sidecar_config(model_path)
        return

    if control_algorithm == "rule_based":
        if control_policy not in _RULE_BASED_REGISTRY:
            raise ValueError(
                f"Unknown rule-based policy '{control_policy}'. "
                f"Registered slugs: {sorted(_RULE_BASED_REGISTRY)}."
            )
        return

    raise ValueError(f"Unknown control_algorithm '{control_algorithm}'. Supported: ann, rule_based.")


def describe_policies(include_source: bool = False) -> list[dict[str, Any]]:
    """Describe every assignable policy: rule-based classes + ANN model files.

    Each entry starts with the identity fields accepted by the charger
    policy-assignment endpoint, followed by the policy's own describe() output.
    The slugs are added here rather than inside describe() so a class and its
    registry key cannot disagree.

    ANN entries are built from file metadata only — no weights are loaded, so
    listing stays cheap no matter how many models are on disk.
    """
    entries: list[dict[str, Any]] = []

    for slug, policy_class in sorted(_RULE_BASED_REGISTRY.items()):
        entry: dict[str, Any] = {
            "control_algorithm": "rule_based",
            "control_policy": slug,
            "is_system_default": slug == DEFAULT_RULE_BASED_SLUG,
            **policy_class().describe(),
        }
        if include_source:
            entry["source_code"] = policy_class.source_code()
        entries.append(entry)

    directory = models_dir()
    model_paths = sorted(directory.glob("*.pth")) if directory.exists() else []

    for model_path in model_paths:
        entry = {
            "control_algorithm": "ann",
            "control_policy": model_path.name,
            "is_system_default": False,
        }
        try:
            entry.update(AnnPolicy.describe_model_file(model_path))
        except Exception as exc:  # a model file is external input; never fail the whole listing
            logger.exception("Could not describe ANN model '%s'.", model_path.name)
            entry["error"] = f"Could not describe this model file: {exc}"
        if include_source:
            entry["source_code"] = AnnPolicy.source_code()
        entries.append(entry)

    return entries


# ── Internal loaders ──────────────────────────────────────────────────────────

def _get_or_load(key: str, loader) -> BasePolicy:
    if key not in _POLICY_CACHE:
        _POLICY_CACHE[key] = loader()
        logger.debug("Policy loaded and cached under key '%s'", key)
    return _POLICY_CACHE[key]


def _load_rule_based(slug: str) -> BasePolicy:
    policy_class = _RULE_BASED_REGISTRY.get(slug)
    if policy_class is None:
        raise ValueError(
            f"Unknown rule-based policy '{slug}'. "
            f"Registered slugs: {sorted(_RULE_BASED_REGISTRY)}."
        )
    return policy_class()


def _load_ann(policy_filename: str) -> AnnPolicy:
    model_path = models_dir() / policy_filename

    if not model_path.exists():
        raise RuntimeError(
            f"ANN model file not found: {model_path.resolve()}. "
            "Place the .pth file in the models directory."
        )

    # Sidecar is mandatory: observation_features define the ANN input layout.
    # Same readers as the policy listing / assignment validation.
    try:
        observation_features, power_levels_kw = require_ann_sidecar_config(model_path)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc

    return AnnPolicy(
        model_path=str(model_path),
        observation_features=observation_features,
        deterministic=True,
        power_levels_kw=power_levels_kw,
        check_input_dim=True,
    )
