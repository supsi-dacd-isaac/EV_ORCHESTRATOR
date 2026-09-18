"""Symmetric encryption for per-pilot secrets (tokens / API keys).

Secrets are stored in the database only as Fernet ciphertext (see the
`pilot_secret` table). The plaintext exists solely in the inbound request and
in memory at query time. Encryption uses a single app-wide master key,
``config.EV_SECRETS_KEY``, provided via the environment (never committed, never
stored in the DB).

Fernet (from the `cryptography` package, already a dependency) gives us
authenticated symmetric encryption: AES-128-CBC for confidentiality plus an
HMAC so a wrong key or tampered ciphertext fails loudly instead of returning
garbage.
"""

from __future__ import annotations

import logging

from cryptography.fernet import Fernet, InvalidToken

from app import config

logger = logging.getLogger(__name__)


class SecretsConfigError(RuntimeError):
    """EV_SECRETS_KEY is missing or is not a valid Fernet key."""


class SecretDecryptError(RuntimeError):
    """Ciphertext could not be decrypted (wrong/rotated key or corrupted data)."""


def secrets_enabled() -> bool:
    """True when a master key is configured (used to short-circuit gracefully)."""
    return bool(config.EV_SECRETS_KEY)


def _fernet() -> Fernet:
    """Build a Fernet instance from the configured master key.

    Raised errors are typed so API/runtime callers can react (503 vs. warn)
    instead of leaking a raw stack trace.
    """
    key = config.EV_SECRETS_KEY
    if not key:
        raise SecretsConfigError(
            "EV_SECRETS_KEY is not set; cannot encrypt or decrypt pilot secrets."
        )
    try:
        # Fernet accepts the urlsafe-base64 key as bytes.
        return Fernet(key.encode("ascii") if isinstance(key, str) else key)
    except (ValueError, TypeError) as exc:
        raise SecretsConfigError(
            "EV_SECRETS_KEY is not a valid Fernet key (expected urlsafe base64, "
            "32 bytes). Regenerate with Fernet.generate_key()."
        ) from exc


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a plaintext secret, returning ASCII ciphertext for DB storage."""
    token = _fernet().encrypt(plaintext.encode("utf-8"))
    return token.decode("ascii")


def decrypt_secret(ciphertext: str) -> str:
    """Decrypt DB ciphertext back to plaintext.

    Raises SecretDecryptError if the key no longer matches the ciphertext.
    """
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise SecretDecryptError(
            "Failed to decrypt secret: the ciphertext does not match the current "
            "EV_SECRETS_KEY (key rotated/changed, or data corrupted)."
        ) from exc
