from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from pydantic import BaseModel

from app.db.session import SessionLocal
from app.services.common.auth import TokenData, get_current_user
from app.services.common.authorization import ensure_charger_access, require_admin
from app.services.orchestrator.obs_layout import (
    DEFAULT_OBSERVATION_FEATURES,
    OBSERVATION_SIZE,
    list_observation_features,
    observation_dim,
)
from app.services.orchestrator.policy.ann_policy import (
    AnnPolicy,
    read_observation_features,
    read_power_levels_kw,
)
from app.services.orchestrator.policy.policy_registry import (
    describe_policies,
    invalidate_policy_cache,
    models_dir,
    validate_policy_reference,
)
from app.models import Pilot
from app.services.orchestrator.signals.wind_excess import is_wind_excess_configured

# Admin-only: upload new model files
admin_router = APIRouter(prefix="/admin/policies", tags=["admin-policies"])

# Charger owners + pilot owners: assign a policy to their own chargers
charger_policy_router = APIRouter(prefix="/chargers", tags=["charger-policy"])

# All authenticated users: read what is available
policies_router = APIRouter(prefix="/policies", tags=["policies"])

# Only allow simple filenames: letters, digits, dot, dash, underscore.
# Blocks path traversal (e.g. "../../etc/passwd") and directory separators.
_SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _validate_filename(filename: Optional[str], expected_suffix: str) -> str:
    if not filename or not _SAFE_FILENAME_RE.match(filename):
        raise HTTPException(
            status_code=400,
            detail="Invalid filename. Only letters, digits, '.', '-', '_' are allowed.",
        )
    if not filename.endswith(expected_suffix):
        raise HTTPException(
            status_code=400,
            detail=f"Expected a '{expected_suffix}' file.",
        )
    return filename


@admin_router.post("/ann/upload")
async def upload_ann_model(
    model_file: UploadFile,
    sidecar_file: UploadFile,
    current_user: TokenData = Depends(get_current_user),
):
    """
    Upload a new ANN model (.pth) and its required sidecar (.json).

    The sidecar must:
    - use the same basename as the model (``model.pth`` → ``model.json``)
    - declare ``observation_features`` (ordered catalog names)
    - optionally declare ``power_levels_kw`` (omit for binary charge/not_charge)

    The assembled feature length must match the model's input dimension.
    No restart needed — the registry loads by filename on next use.
    """
    require_admin(current_user)

    model_filename = _validate_filename(model_file.filename, ".pth")
    sidecar_filename = _validate_filename(sidecar_file.filename, ".json")
    if Path(sidecar_filename).stem != Path(model_filename).stem:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Sidecar filename stem must match the model stem "
                f"('{Path(model_filename).stem}.json')."
            ),
        )

    model_bytes = await model_file.read()
    sidecar_bytes = await sidecar_file.read()

    try:
        sidecar = json.loads(sidecar_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Sidecar is not valid UTF-8 JSON: {exc}",
        ) from exc

    if not isinstance(sidecar, dict):
        raise HTTPException(status_code=400, detail="Sidecar JSON must be an object.")

    observation_features, obs_problems = read_observation_features(sidecar)
    if observation_features is None:
        raise HTTPException(
            status_code=400,
            detail="Invalid observation_features: " + "; ".join(obs_problems),
        )

    power_levels_kw, power_problems = read_power_levels_kw(sidecar)

    directory = models_dir()
    directory.mkdir(parents=True, exist_ok=True)
    model_path = directory / model_filename
    sidecar_path = directory / sidecar_filename

    model_path.write_bytes(model_bytes)
    sidecar_path.write_bytes(sidecar_bytes)

    # Verify network input dim against sidecar features; roll back on mismatch.
    try:
        policy = AnnPolicy(
            model_path=str(model_path),
            observation_features=observation_features,
            deterministic=True,
            power_levels_kw=power_levels_kw,
            check_input_dim=True,
        )
        architecture = policy._architecture()
    except Exception as exc:
        model_path.unlink(missing_ok=True)
        sidecar_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=400,
            detail=f"Uploaded ANN failed validation: {exc}",
        ) from exc

    # Drop any stale cached instance so the next request loads the new file.
    invalidate_policy_cache("ann", model_filename)

    response = {
        "status": "uploaded",
        "model_filename": model_filename,
        "sidecar_filename": sidecar_filename,
        "control_policy_slug": model_filename,
        "observation_features": observation_features,
        "observation_dim_expected": observation_dim(observation_features),
        "observation_dim_model": architecture.get("observation_dim"),
        "power_levels_kw": power_levels_kw,
        "is_binary": power_levels_kw is None,
    }
    if power_problems:
        response["warnings"] = power_problems
    return response


@admin_router.get("/observation-features")
def get_observation_features(
    current_user: TokenData = Depends(get_current_user),
):
    """List every observation feature name that ANN sidecars may declare.

    Admin-only catalog for writing ``observation_features`` in a model sidecar.
    Includes size, description, whether each name is in the default full layout,
    and the default ordered feature list (historical 44-dim observation).
    """
    require_admin(current_user)

    features = list_observation_features()
    return {
        "count": len(features),
        "default_observation_dim": OBSERVATION_SIZE,
        "default_observation_features": list(DEFAULT_OBSERVATION_FEATURES),
        "features": features,
        "example_sidecar": {
            "name": "Example ANN",
            "description": "Binary model using a small feature subset.",
            "observation_features": [
                "fully_charged",
                "time_curr_sin",
                "time_curr_cos",
                "connected_time_relative",
                "energy_charged_rel_needed",
                "community_load",
            ],
            "power_levels_kw": None,
        },
        "notes": [
            "Copy names from 'features' (or from 'default_observation_features' for the "
            "full historical layout) into the sidecar field observation_features. "
            "Order must match training order.",
            "Example: the 'example_sidecar' object above is a valid .json shape. "
            "Omit power_levels_kw (or set it to a kW list) depending on binary vs "
            "discrete power-level models. Sum of sizes for that example list is "
            "1+1+1+1+1+24 = 29, so the ANN input dim must be 29.",
            "The assembled length (sum of feature sizes) must equal the ANN "
            "model's input dimension.",
            "community_load is a vector whose size equals "
            "BASELOAD_FORECAST_HORIZON_STEPS.",
            "Upload models with POST /admin/policies/ann/upload (sidecar required; "
            "basename must match, e.g. my_model.pth + my_model.json).",
        ],
    }


@policies_router.get("")
def list_policies(
    include_source: bool = False,
    current_user: TokenData = Depends(get_current_user),
):
    """List every assignable policy together with its description.

    One call returns the whole catalogue: the rule-based policies from the
    registry and every ANN model file found in the models directory. Each entry
    carries the `control_algorithm` / `control_policy` pair accepted by
    PATCH /chargers/{charger_id}/policy, so the response doubles as the menu of
    valid assignments, plus what the policy does, whether it is binary and which
    power levels it can select.

    Pass `include_source=true` to also get the Python source of each policy class.
    """
    policies = describe_policies(include_source=include_source)

    return {
        "count": len(policies),
        "policies": policies,
        "notes": [
            "Assign an entry to a charger with PATCH /chargers/{charger_id}/policy, "
            "passing its control_algorithm and control_policy values.",
            "Setting both fields to null reverts the charger to the entry flagged "
            "is_system_default.",
            "Every decision then passes through the correction filter: charging is "
            "blocked when the vehicle is disconnected or fully charged, and any "
            "suggested power is clamped to the charger nominal power.",
        ],
    }


@policies_router.get("/ann")
def list_ann_models(
    current_user: TokenData = Depends(get_current_user),
):
    """List .pth files currently available in the models directory."""
    directory = models_dir()
    if not directory.exists():
        return {"models": []}

    return {
        "models": sorted(p.name for p in directory.glob("*.pth")),
    }


class ChargerPolicyAssignment(BaseModel):
    control_algorithm: Optional[str] = None  # "ann" | "rule_based" | None
    control_policy: Optional[str] = None     # e.g. "policy_....pth" | "wind_max" | None


@charger_policy_router.patch("/{charger_id}/policy")
def assign_charger_policy(
    charger_id: UUID,
    payload: ChargerPolicyAssignment,
    current_user: TokenData = Depends(get_current_user),
):
    """
    Assign a control algorithm + policy to a charger.
    Allowed for: admins, the charger's owner, and the owner of its pilot.
    Setting both fields to null reverts the charger to the system default policy.
    """
    db = SessionLocal()
    try:
        charger = ensure_charger_access(
            db,
            current_user,
            charger_id,
            allow_charger_owner=True,
            allow_pilot_owner=True,
            allow_guest=False,
        )

        try:
            validate_policy_reference(payload.control_algorithm, payload.control_policy)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        charger.control_algorithm = payload.control_algorithm
        charger.control_policy = payload.control_policy
        db.commit()

        result = {
            "charger_id": str(charger_id),
            "control_algorithm": charger.control_algorithm,
            "control_policy": charger.control_policy,
        }

        # Soft warning: wind_max is allowed without config, but excess will be 0.0.
        if (
            payload.control_algorithm == "rule_based"
            and payload.control_policy == "wind_max"
        ):
            pilot = (
                db.query(Pilot).filter(Pilot.id == charger.id_pilot).first()
                if charger.id_pilot
                else None
            )
            if not is_wind_excess_configured(pilot):
                result["warnings"] = [
                    "Pilot has no wind_excess config in policy_signals "
                    "(missing, disabled, or pilot unset); decisions will use "
                    "wind_excess_kw=0.0 until configured."
                ]

        return result
    finally:
        db.close()

