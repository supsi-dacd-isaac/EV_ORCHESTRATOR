"""Resolve a pilot's named external connections (pilot.data_sources).

`data_sources` is a NON-secret registry, e.g.::

    {
      "example_influx": {
        "type": "influxdb",
        "url": "https://.../influxdb/",
        "org": "interped",
        "bucket": "interped",
        "token_secret": "example_influx_token"
      }
    }

A policy signal (e.g. wind_excess) references one of these by name via its
"source" field. The actual credential is stored encrypted in pilot_secret and
fetched here by the connection's "token_secret" name.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.models import Pilot
from app.services.common.pilot_secrets import get_pilot_secret_value

logger = logging.getLogger(__name__)

# Keys every connection may have, plus the keys allowed for each ``type``.
# Writes fail closed: any extra key (e.g. "token", "my_token") is rejected.
_COMMON_CONN_KEYS = {"type", "description"}
ALLOWED_CONN_KEYS_BY_TYPE: Dict[str, set[str]] = {
    "influxdb": {"url", "org", "bucket", "token_secret"},
    # Demand / generation forecasters (Phase 4B). Credentials via auth_secret
    # → pilot_secret row; auth describes how to send the key (header or body).
    "rest_api": {"base_url", "auth_secret", "auth"},
}


def allowed_keys_for_type(conn_type: str) -> set[str]:
    return _COMMON_CONN_KEYS | ALLOWED_CONN_KEYS_BY_TYPE.get(conn_type, set())


def validate_data_sources(data_sources: Any) -> None:
    """Raise ValueError if data_sources is not a dict of known connection shapes.

    ``None`` is allowed (column optional). Each connection must be a dict with
    a known ``type`` and only allowlisted keys for that type.
    """
    if data_sources is None:
        return
    if not isinstance(data_sources, dict):
        raise ValueError("data_sources must be a JSON object keyed by connection name")

    for conn_name, conn in data_sources.items():
        if not isinstance(conn, dict):
            raise ValueError(f"data_sources[{conn_name!r}] must be a JSON object")
        conn_type = conn.get("type")
        if conn_type not in ALLOWED_CONN_KEYS_BY_TYPE:
            known = ", ".join(sorted(ALLOWED_CONN_KEYS_BY_TYPE))
            raise ValueError(
                f"data_sources[{conn_name!r}] has unknown type {conn_type!r}; "
                f"allowed types: {known}"
            )
        allowed = allowed_keys_for_type(conn_type)
        extra = set(conn.keys()) - allowed
        if extra:
            raise ValueError(
                f"data_sources[{conn_name!r}] has unknown key(s) {sorted(extra)}; "
                f"allowed for type {conn_type!r}: {sorted(allowed)}"
            )


def get_data_sources(pilot: Optional[Pilot]) -> Dict[str, Any]:
    """Return the pilot's data_sources dict, or {} if missing/malformed."""
    if pilot is None or not isinstance(pilot.data_sources, dict):
        return {}
    return pilot.data_sources


def resolve_connection(pilot: Optional[Pilot], name: str) -> Optional[Dict[str, Any]]:
    """Return the connection config named ``name``, or None if not defined."""
    connection = get_data_sources(pilot).get(name)
    return connection if isinstance(connection, dict) else None


def resolve_connection_token(
    db: Session,
    pilot: Pilot,
    connection: Dict[str, Any],
) -> Optional[str]:
    """Return the decrypted token for a connection, or None if none is configured.

    Reads ``token_secret`` (influxdb) or ``auth_secret`` (rest_api) and looks up
    the matching pilot_secret row. May raise SecretsConfigError /
    SecretDecryptError if the master key is missing or wrong — callers in the
    runtime path should catch and degrade gracefully.
    """
    secret_name = connection.get("token_secret") or connection.get("auth_secret")
    if not secret_name:
        return None
    return get_pilot_secret_value(db, pilot.id, secret_name)
