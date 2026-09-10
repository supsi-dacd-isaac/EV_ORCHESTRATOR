from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Union
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.models import SystemErrors

logger = logging.getLogger(__name__)


def _coerce_uuid(value: Optional[Union[UUID, str]]) -> Optional[UUID]:
    if value is None or isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def log_system_error(
    db: Session,
    *,
    source: str,
    error: Exception,
    charger_id: Optional[Union[UUID, str]] = None,
    session_id: Optional[Union[UUID, str]] = None,
    context: Optional[Dict[str, Any]] = None,
) -> None:
    """Record an unexpected failure to `system_errors` without raising further.

    Adds the row to `db` — the caller is responsible for committing it.
    Never raises: a failure while logging a failure must not mask the original.
    """
    logger.exception("system error in %s: %s", source, error)
    try:
        db.add(
            SystemErrors(
                id=uuid4(),
                source=source,
                charger_id=_coerce_uuid(charger_id),
                session_id=_coerce_uuid(session_id),
                error_message=str(error)[:2000],
                context=context,
            )
        )
    except Exception:
        logger.exception("Failed to record system_errors row for source=%s", source)
