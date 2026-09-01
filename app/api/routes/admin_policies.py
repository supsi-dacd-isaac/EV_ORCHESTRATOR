from __future__ import annotations

import re
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from pydantic import BaseModel

from app.db.session import SessionLocal
from app.services.common.auth import TokenData, get_current_user
from app.services.common.authorization import ensure_charger_access, require_admin
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
    sidecar_file: Optional[UploadFile] = None,
    current_user: TokenData = Depends(get_current_user),
):
    """
    Upload a new ANN model (.pth) and optional sidecar (.json) describing
    power_levels_kw. Saves to the models directory used by the policy registry.
    No restart needed — the registry loads by filename on next use.
    """
    require_admin(current_user)

    model_filename = _validate_filename(model_file.filename, ".pth")
    directory = models_dir()
    directory.mkdir(parents=True, exist_ok=True)

    model_path = directory / model_filename
    model_path.write_bytes(await model_file.read())

    sidecar_filename = None
    if sidecar_file is not None:
        sidecar_filename = _validate_filename(sidecar_file.filename, ".json")
        sidecar_path = directory / sidecar_filename
        sidecar_path.write_bytes(await sidecar_file.read())

    # Drop any stale cached instance so the next request loads the new file.
    invalidate_policy_cache("ann", model_filename)

    return {
        "status": "uploaded",
        "model_filename": model_filename,
        "sidecar_filename": sidecar_filename,
        "control_policy_slug": model_filename,
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

