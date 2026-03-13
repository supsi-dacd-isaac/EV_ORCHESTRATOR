from __future__ import annotations

from typing import Iterable
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models import Actions, Chargers, ChargingSessions, Pilot
from app.services.common.auth import TokenData


def _forbidden(detail: str = "Forbidden") -> None:
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def _normalize_role(role: str | None) -> str:
    normalized = (role or "").strip().lower()
    if normalized == "observer":
        return "guest"
    return normalized


def _role(current_user: TokenData) -> str:
    return _normalize_role(current_user.role)


def get_role(current_user: TokenData) -> str:
    return _role(current_user)


def is_admin(current_user: TokenData) -> bool:
    return _role(current_user) == "admin"


def is_user_role(current_user: TokenData) -> bool:
    return _role(current_user) in {"user", "user-adv"}


def is_guest_role(current_user: TokenData) -> bool:
    return _role(current_user) == "guest"


def require_roles(current_user: TokenData, *roles: str) -> None:
    normalized_roles = {_normalize_role(r) for r in roles}
    if "user" in normalized_roles:
        normalized_roles.add("user-adv")
    if _role(current_user) not in normalized_roles:
        _forbidden()


def require_admin(current_user: TokenData) -> None:
    require_roles(current_user, "admin")


def require_admin_or_owner(current_user: TokenData, owner_id: UUID) -> None:
    if _role(current_user) == "admin":
        return
    if current_user.owner_id != owner_id:
        _forbidden()


def ensure_pilot_access(
    db: Session,
    current_user: TokenData,
    pilot_id: UUID,
    *,
    allow_user: bool = True,
    allow_guest: bool = False,
    require_user_ownership: bool = True,
) -> Pilot:
    pilot = db.query(Pilot).filter(Pilot.id == pilot_id).first()
    if not pilot:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pilot not found")

    role = _role(current_user)
    if role == "admin":
        return pilot

    if role == "guest":
        if allow_guest:
            return pilot
        _forbidden()

    if is_user_role(current_user):
        if not allow_user:
            _forbidden()
        if require_user_ownership and pilot.id_owner != current_user.owner_id:
            _forbidden()
        return pilot

    _forbidden()


def ensure_charger_access(
    db: Session,
    current_user: TokenData,
    charger_id: UUID,
    *,
    allow_charger_owner: bool = True,
    allow_pilot_owner: bool = True,
    allow_guest: bool = False,
) -> Chargers:
    charger = db.query(Chargers).filter(Chargers.id == charger_id).first()
    if not charger:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Charger not found")

    role = _role(current_user)
    if role == "admin":
        return charger

    if role == "guest":
        if allow_guest:
            return charger
        _forbidden()

    if not is_user_role(current_user):
        _forbidden()

    is_owner = charger.id_owner == current_user.owner_id
    is_pilot_owner = False
    if charger.id_pilot:
        is_pilot_owner = (
            db.query(Pilot)
            .filter(Pilot.id == charger.id_pilot, Pilot.id_owner == current_user.owner_id)
            .first()
            is not None
        )

    if (allow_charger_owner and is_owner) or (allow_pilot_owner and is_pilot_owner):
        return charger

    _forbidden()


def ensure_session_access(
    db: Session,
    current_user: TokenData,
    session_id: UUID,
    *,
    allow_charger_owner: bool = True,
    allow_pilot_owner: bool = True,
    allow_guest: bool = False,
) -> ChargingSessions:
    session = db.query(ChargingSessions).filter(ChargingSessions.id == session_id).first()
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    if not session.id_charger:
        _forbidden()

    ensure_charger_access(
        db,
        current_user,
        session.id_charger,
        allow_charger_owner=allow_charger_owner,
        allow_pilot_owner=allow_pilot_owner,
        allow_guest=allow_guest,
    )
    return session


def ensure_action_access(
    db: Session,
    current_user: TokenData,
    action_id: UUID,
    *,
    allow_charger_owner: bool = True,
    allow_pilot_owner: bool = True,
    allow_guest: bool = False,
) -> Actions:
    action = db.query(Actions).filter(Actions.id == action_id).first()
    if not action:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Action not found")
    if not action.id_cs:
        _forbidden()

    ensure_session_access(
        db,
        current_user,
        action.id_cs,
        allow_charger_owner=allow_charger_owner,
        allow_pilot_owner=allow_pilot_owner,
        allow_guest=allow_guest,
    )
    return action


def get_accessible_charger_ids(
    db: Session,
    current_user: TokenData,
    *,
    allow_charger_owner: bool = True,
    allow_pilot_owner: bool = True,
    allow_guest: bool = False,
) -> list[UUID]:
    role = _role(current_user)

    if role == "admin":
        return [row.id for row in db.query(Chargers.id).all()]

    if role == "guest":
        if allow_guest:
            return [row.id for row in db.query(Chargers.id).all()]
        _forbidden()

    if not is_user_role(current_user):
        _forbidden()

    filters: list = []
    if allow_charger_owner:
        filters.append(Chargers.id_owner == current_user.owner_id)

    if allow_pilot_owner:
        filters.append(
            Chargers.id_pilot.in_(
                db.query(Pilot.id).filter(Pilot.id_owner == current_user.owner_id)
            )
        )

    if not filters:
        return []

    return [row.id for row in db.query(Chargers.id).filter(or_(*filters)).all()]


def ensure_owner_in_ids(owner_id: UUID, owner_ids: Iterable[UUID]) -> None:
    if owner_id not in set(owner_ids):
        _forbidden()
