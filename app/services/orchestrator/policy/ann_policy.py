from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import torch
import torch.nn as nn

from app.services.orchestrator.observation_builder import select_observation_from_default
from app.services.orchestrator.obs_layout import (
    observation_dim,
    validate_feature_names,
)

from .actor import Actor
from .base_policy import BasePolicy, PolicyDecision

logger = logging.getLogger(__name__)

# A model file "model.pth" must be accompanied by "model.json" carrying metadata.
SIDECAR_SUFFIX = ".json"


def sidecar_path_for(model_path: Path) -> Path:
    return model_path.with_suffix(SIDECAR_SUFFIX)


def read_model_sidecar(model_path: Path) -> Dict[str, Any]:
    """Return the sidecar metadata for a model file, or {} when absent/unreadable."""
    path = sidecar_path_for(model_path)
    if not path.exists():
        return {}

    try:
        content = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read model sidecar %s: %s", path, exc)
        return {}

    if not isinstance(content, dict):
        logger.warning("Model sidecar %s is not a JSON object — ignoring it.", path)
        return {}
    return content


def read_power_levels_kw(sidecar: Dict[str, Any]) -> Tuple[Optional[List[float]], List[str]]:
    """Extract power_levels_kw from sidecar metadata.

    Returns (power_levels_kw, problems). A None value means binary mode, which is
    also the fallback for malformed metadata: an unusable list must not reach
    compute_action(), where it would raise on every inference call instead.
    """
    raw = sidecar.get("power_levels_kw")
    if raw is None:
        return None, []

    if not isinstance(raw, (list, tuple)) or len(raw) == 0:
        return None, ["Sidecar 'power_levels_kw' is not a non-empty list - running in binary mode."]

    try:
        levels = [float(value) for value in raw]
    except (TypeError, ValueError):
        return None, ["Sidecar 'power_levels_kw' holds non-numeric values - running in binary mode."]

    if any(value < 0 for value in levels):
        return None, ["Sidecar 'power_levels_kw' holds negative values - running in binary mode."]

    return levels, []


def read_observation_features(
    sidecar: Dict[str, Any],
) -> Tuple[Optional[List[str]], List[str]]:
    """Extract required observation_features from sidecar metadata.

    Returns (feature_names, problems). feature_names is None when the field is
    missing or unusable — callers must treat that as a hard configuration error.
    """
    raw = sidecar.get("observation_features")
    if raw is None:
        return None, [
            "Sidecar is missing required 'observation_features' "
            "(ordered list of catalog feature names)."
        ]

    if not isinstance(raw, (list, tuple)):
        return None, ["Sidecar 'observation_features' must be a list of feature names."]

    names = [str(item) for item in raw]
    problems = validate_feature_names(names)
    if problems:
        return None, problems
    return names, []


def require_ann_sidecar_config(model_path: Path) -> Tuple[List[str], Optional[List[float]]]:
    """Load and validate sidecar config required to run an ANN model.

    Raises ValueError when the sidecar is missing or observation_features is invalid.
    power_levels_kw may still be None (binary mode).
    """
    sidecar_file = sidecar_path_for(model_path)
    if not sidecar_file.exists():
        raise ValueError(
            f"ANN model '{model_path.name}' requires sidecar '{sidecar_file.name}'."
        )

    sidecar = read_model_sidecar(model_path)
    if not sidecar:
        raise ValueError(
            f"ANN sidecar '{sidecar_file.name}' is missing, unreadable, or not a JSON object."
        )

    observation_features, obs_problems = read_observation_features(sidecar)
    if observation_features is None:
        raise ValueError(
            f"ANN sidecar '{sidecar_file.name}' has invalid observation_features: "
            + "; ".join(obs_problems)
        )

    power_levels_kw, power_problems = read_power_levels_kw(sidecar)
    for problem in power_problems:
        logger.warning("ANN model '%s': %s", model_path.name, problem)

    return observation_features, power_levels_kw


def _action_mapping_rules(power_levels_kw: Optional[List[float]]) -> List[str]:
    """Plain-language mapping from model output class to charging action."""
    rules = ["The output class with the highest score wins (argmax over the model outputs)."]

    if power_levels_kw is None:
        rules += [
            "Output class 0 -> not_charge.",
            "Output class 1 -> charge. No power level is suggested, so the charging "
            "power is whatever the charger delivers up to its nominal power.",
        ]
        return rules

    for index, kw in enumerate(power_levels_kw):
        action = "not_charge" if kw <= 0 else f"charge at {kw:g} kW"
        rules.append(f"Output class {index} -> {action}.")
    return rules


class AnnPolicy(BasePolicy):
    """Neural-network policy wrapping the Actor.

    Binary mode (power_levels_kw=None): output class 0 = not_charge, 1 = charge.
    Power-level mode: each output class index maps to a kW value in power_levels_kw,
    e.g. [0.0, 3.7, 7.4] for a 3-class model. Both modes share the same Actor code.

    ``observation_features`` is the ordered sidecar list of catalog names. The
    orchestrator still passes the default full observation; this policy selects
    and reorders the declared subset before inference.
    """

    def __init__(
        self,
        model_path: str,
        observation_features: Sequence[str],
        deterministic: bool = True,
        power_levels_kw: Optional[List[float]] = None,
        *,
        check_input_dim: bool = True,
    ):
        problems = validate_feature_names(observation_features)
        if problems:
            raise ValueError("; ".join(problems))

        self.model_path = Path(model_path)
        self.observation_features: List[str] = list(observation_features)
        self.expected_observation_dim = observation_dim(self.observation_features)
        self.actor = Actor(model_path)
        self.deterministic = deterministic
        # None → binary; list → discrete power levels indexed by action class
        self.power_levels_kw = power_levels_kw

        if check_input_dim:
            self._assert_input_dim_matches()

    def _assert_input_dim_matches(self) -> None:
        architecture = self._architecture()
        model_dim = architecture.get("observation_dim")
        if model_dim is None:
            return
        if model_dim != self.expected_observation_dim:
            raise ValueError(
                f"ANN model '{self.model_path.name}' expects observation_dim={model_dim}, "
                f"but sidecar observation_features assemble to "
                f"{self.expected_observation_dim} "
                f"({len(self.observation_features)} features)."
            )

    def compute_action(
        self,
        obs: np.ndarray,
        context: Optional[Dict[str, Any]] = None,
    ) -> PolicyDecision:
        # context is unused: this ANN only relies on the normalized obs vector.
        model_obs = select_observation_from_default(obs, self.observation_features)
        if model_obs.shape != (self.expected_observation_dim,):
            raise ValueError(
                f"Assembled ANN observation has shape {model_obs.shape}, "
                f"expected ({self.expected_observation_dim},)."
            )

        with torch.no_grad():
            if model_obs.ndim == 1:
                model_obs = model_obs[None, :]

            obs_tensor = torch.from_numpy(model_obs)
            logits = self.actor(obs_tensor)

            if self.deterministic:
                action = torch.argmax(logits, dim=-1)
            else:
                probs = torch.distributions.Categorical(logits=logits)
                action = probs.sample()

            action_index = int(action.item())

        if self.power_levels_kw is not None:
            kw = self.power_levels_kw[action_index]
            return PolicyDecision(
                action="charge" if kw > 0 else "not_charge",
                suggested_power_kw=kw if kw > 0 else None,
            )

        return PolicyDecision(
            action="charge" if action_index else "not_charge",
            suggested_power_kw=None,
        )

    @classmethod
    def describe_model_file(cls, model_path: Path) -> Dict[str, Any]:
        """Describe a model from its file metadata, without loading the weights.

        Used by the policy listing so that showing N models costs N small file
        reads instead of N torch deserialisations.
        """
        sidecar = read_model_sidecar(model_path)
        power_levels_kw, problems = read_power_levels_kw(sidecar)
        observation_features, obs_problems = read_observation_features(sidecar)
        problems.extend(obs_problems)
        sidecar_file = sidecar_path_for(model_path)

        if not sidecar_file.exists():
            problems.append(
                f"Required sidecar '{sidecar_file.name}' is missing. "
                "ANN models cannot run without observation_features."
            )
        elif sidecar_file.exists() and not sidecar:
            problems.append(
                f"Sidecar '{sidecar_file.name}' exists but holds no usable metadata "
                "(unreadable, empty, or not a JSON object)."
            )

        expected_dim = (
            observation_dim(observation_features) if observation_features is not None else None
        )

        try:
            stat = model_path.stat()
            size_bytes: Optional[int] = stat.st_size
            modified_utc: Optional[str] = datetime.fromtimestamp(
                stat.st_mtime, tz=timezone.utc
            ).isoformat()
        except OSError:
            size_bytes, modified_utc = None, None

        description: Dict[str, Any] = {
            "name": sidecar.get("name") or f"ANN policy ({model_path.name})",
            "summary": sidecar.get("description")
            or (
                "Trained neural-network policy. Reads the normalized observation "
                "features declared in its sidecar and picks the charging action with "
                "the highest score."
            ),
            "is_binary": power_levels_kw is None,
            "power_levels_kw": power_levels_kw,
            "power_selection": "binary" if power_levels_kw is None else "discrete_levels",
            "rules": _action_mapping_rules(power_levels_kw),
            "observation_features": observation_features,
            "observation_dim_expected": expected_dim,
            "parameters": {
                "model_filename": model_path.name,
                "model_file_size_bytes": size_bytes,
                "model_file_modified_utc": modified_utc,
                "sidecar_filename": sidecar_file.name if sidecar_file.exists() else None,
                "sidecar": sidecar,
                "deterministic": True,
                "num_action_classes": 2 if power_levels_kw is None else len(power_levels_kw),
                "observation_features": observation_features,
                "observation_dim_expected": expected_dim,
            },
            "notes": [
                "Taken from the model file and its sidecar .json only: the network "
                "weights are not loaded here, so the number of output classes is the "
                "one declared by the sidecar and is not verified against the model.",
                "observation_features is required and must list catalog names in "
                "training order.",
                "Binary mode is assumed when no sidecar declares power_levels_kw.",
            ],
        }

        if problems:
            description["warnings"] = problems
        return description

    def describe(self) -> Dict[str, Any]:
        """Describe this loaded instance, including its real architecture."""
        description = self.describe_model_file(self.model_path)
        architecture = self._architecture()

        # The instance is authoritative: power levels / features may have been
        # passed in directly rather than only read from the sidecar.
        description["is_binary"] = self.power_levels_kw is None
        description["power_levels_kw"] = self.power_levels_kw
        description["power_selection"] = (
            "binary" if self.power_levels_kw is None else "discrete_levels"
        )
        description["rules"] = _action_mapping_rules(self.power_levels_kw)
        description["observation_features"] = self.observation_features
        description["observation_dim_expected"] = self.expected_observation_dim
        description["parameters"].update(
            {
                "deterministic": self.deterministic,
                "device": str(self.actor.device),
                "num_action_classes": architecture.get("num_output_classes")
                or description["parameters"]["num_action_classes"],
                "observation_features": self.observation_features,
                "observation_dim_expected": self.expected_observation_dim,
                **architecture,
            }
        )
        description["notes"] = ["Read from the loaded model, so the architecture is exact."]

        num_classes = architecture.get("num_output_classes")
        expected = 2 if self.power_levels_kw is None else len(self.power_levels_kw)
        if num_classes is not None and num_classes != expected:
            description.setdefault("warnings", []).append(
                f"The model has {num_classes} output classes but the configuration "
                f"expects {expected}. Actions may be mapped to the wrong power level."
            )

        model_dim = architecture.get("observation_dim")
        if model_dim is not None and model_dim != self.expected_observation_dim:
            description.setdefault("warnings", []).append(
                f"The model has observation_dim={model_dim} but sidecar features "
                f"assemble to {self.expected_observation_dim}."
            )
        return description

    def _architecture(self) -> Dict[str, Any]:
        """Layer sizes read back from the loaded network; {} if the shape is unexpected."""
        try:
            hidden_layers = [m for m in self.actor.network if isinstance(m, nn.Linear)]
            output_layer = self.actor.actor
            first_layer = hidden_layers[0] if hidden_layers else output_layer
            return {
                "observation_dim": first_layer.in_features,
                "hidden_layer_sizes": [layer.out_features for layer in hidden_layers],
                "num_output_classes": output_layer.out_features,
            }
        except (AttributeError, IndexError):
            logger.warning("Could not read the architecture of %s.", self.model_path)
            return {}
