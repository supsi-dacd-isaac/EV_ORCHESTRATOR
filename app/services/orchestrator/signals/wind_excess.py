"""Wind-excess signal: config on pilot, value from an external DB (placeholder).

The live kW value used at decision time is snapshotted into
``actions.decision_context``; this module only knows how to fetch it.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.models import Pilot

logger = logging.getLogger(__name__)

SIGNAL_KEY = "wind_excess"


def get_wind_excess_config(pilot: Optional[Pilot]) -> Optional[Dict[str, Any]]:
    """Return the ``wind_excess`` block from ``pilot.policy_signals``, or None."""
    if pilot is None or not isinstance(pilot.policy_signals, dict):
        return None
    cfg = pilot.policy_signals.get(SIGNAL_KEY)
    if not isinstance(cfg, dict):
        return None
    if cfg.get("enabled") is False:
        return None
    return cfg


def is_wind_excess_configured(pilot: Optional[Pilot]) -> bool:
    return get_wind_excess_config(pilot) is not None


def get_wind_excess_kw(
    db: Session,
    pilot_id: Optional[UUID],
    at_time: datetime,
) -> Optional[float]:
    """Fetch wind excess (kW) for a pilot at ``at_time``.

    Returns
    -------
    float
        Queried excess power in kW.
    None
        Pilot missing, signal not configured, or external query not yet
        implemented / failed — caller should treat as 0.0 and warn.
    """
    if pilot_id is None:
        return None

    pilot = db.query(Pilot).filter(Pilot.id == pilot_id).first()
    config = get_wind_excess_config(pilot)
    if config is None:
        return None

    return _query_external_wind_excess(config, at_time)


def _query_external_wind_excess(
    config: Dict[str, Any],
    at_time: datetime,
) -> Optional[float]:
    """PLACEHOLDER — replace the body with a real query to the external DB.

    Expected config shape (see migration ``add_pilot_policy_signals.sql``)::

        {
          "enabled": true,
          "source": "external_db",
          "connection": {"url": ..., "database": ..., "schema": ..., "table": ...},
          "query": {"value_column": ..., "time_column": ..., "extra": {}}
        }

    Should return the wind-excess kW at or just before ``at_time``, or None
    when the value cannot be obtained.
    """
    # --- FILL IN / REPLACE FROM HERE -----------------------------------------
    connection = config.get("connection") or {}
    query = config.get("query") or {}
    logger.warning(
        "wind_excess external query is not implemented yet "
        "(source=%r url=%r table=%r value_column=%r at_time=%s) — returning None",
        config.get("source"),
        connection.get("url"),
        connection.get("table"),
        query.get("value_column"),
        at_time.isoformat(),
    )
    return None
    # --- FILL IN / REPLACE UNTIL HERE ----------------------------------------
