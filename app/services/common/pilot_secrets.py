"""Database access for encrypted per-pilot secrets (pilot_secret table).

Thin CRUD layer on top of `secrets.py`: it encrypts on write and decrypts on
read so callers (API endpoints, signal resolvers) never touch ciphertext or the
master key directly. Mutating helpers add/modify rows on the given session but
do NOT commit — the caller controls the transaction boundary.
"""

from __future__ import annotations

import datetime
from typing import List, Optional
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.models import PilotSecret
from app.services.common.secrets import decrypt_secret, encrypt_secret


def _now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def set_pilot_secret(db: Session, id_pilot: UUID, name: str, value: str) -> PilotSecret:
    """Create or rotate the secret named ``name`` for a pilot.

    Upserts on (id_pilot, name): a new secret inserts a row, an existing one has
    its ciphertext and updated_at refreshed. Encryption happens here, so the
    plaintext ``value`` never leaves this call.
    """
    ciphertext = encrypt_secret(value)
    row = (
        db.query(PilotSecret)
        .filter(PilotSecret.id_pilot == id_pilot, PilotSecret.name == name)
        .first()
    )
    if row is None:
        row = PilotSecret(
            id=uuid4(),
            id_pilot=id_pilot,
            name=name,
            ciphertext=ciphertext,
        )
        db.add(row)
    else:
        row.ciphertext = ciphertext
        row.updated_at = _now_utc()
    return row


def list_pilot_secrets(db: Session, id_pilot: UUID) -> List[PilotSecret]:
    """Return the pilot's secret rows (metadata only; values stay encrypted)."""
    return (
        db.query(PilotSecret)
        .filter(PilotSecret.id_pilot == id_pilot)
        .order_by(PilotSecret.name)
        .all()
    )


def get_pilot_secret_value(db: Session, id_pilot: UUID, name: str) -> Optional[str]:
    """Return the decrypted plaintext for a pilot secret, or None if not set.

    Raises SecretsConfigError / SecretDecryptError (from secrets.py) if the
    master key is missing or no longer matches the stored ciphertext.
    """
    row = (
        db.query(PilotSecret)
        .filter(PilotSecret.id_pilot == id_pilot, PilotSecret.name == name)
        .first()
    )
    if row is None:
        return None
    return decrypt_secret(row.ciphertext)


def delete_pilot_secret(db: Session, id_pilot: UUID, name: str) -> bool:
    """Delete a pilot secret. Returns True if a row was removed."""
    row = (
        db.query(PilotSecret)
        .filter(PilotSecret.id_pilot == id_pilot, PilotSecret.name == name)
        .first()
    )
    if row is None:
        return False
    db.delete(row)
    return True
