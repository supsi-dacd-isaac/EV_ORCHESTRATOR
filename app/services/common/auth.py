import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import bcrypt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from app.config import (
    AUTH_ACCESS_TOKEN_EXPIRE_MINUTES,
    AUTH_ALGORITHM,
    AUTH_PBKDF2_ITERATIONS,
    AUTH_SECRET_KEY,
)

security = HTTPBearer()
ACCESS_TOKEN_EXPIRE_MINUTES = AUTH_ACCESS_TOKEN_EXPIRE_MINUTES


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, AUTH_PBKDF2_ITERATIONS)
    salt_b64 = base64.urlsafe_b64encode(salt).decode("ascii")
    digest_b64 = base64.urlsafe_b64encode(digest).decode("ascii")
    return f"pbkdf2_sha256${AUTH_PBKDF2_ITERATIONS}${salt_b64}${digest_b64}"


def is_password_hashed(password_value: str) -> bool:
    # Accept legacy bcrypt and current PBKDF2 formats.
    return password_value.startswith("$2") or password_value.startswith("pbkdf2_sha256$")


def _verify_pbkdf2(plain_password: str, stored_password: str) -> bool:
    try:
        _, iterations_str, salt_b64, digest_b64 = stored_password.split("$", 3)
        iterations = int(iterations_str)
        salt = base64.urlsafe_b64decode(salt_b64.encode("ascii"))
        expected_digest = base64.urlsafe_b64decode(digest_b64.encode("ascii"))
    except (ValueError, TypeError):
        return False

    candidate_digest = hashlib.pbkdf2_hmac(
        "sha256", plain_password.encode("utf-8"), salt, iterations
    )
    return hmac.compare_digest(candidate_digest, expected_digest)


def verify_password(plain_password: str, stored_password: str) -> bool:
    if stored_password.startswith("pbkdf2_sha256$"):
        return _verify_pbkdf2(plain_password, stored_password)

    if stored_password.startswith("$2"):
        try:
            return bcrypt.checkpw(
                plain_password.encode("utf-8"),
                stored_password.encode("utf-8"),
            )
        except ValueError:
            return False

    # Backward compatibility for legacy plaintext rows in owners table.
    return plain_password == stored_password


def create_access_token(data: dict[str, Any], expires_delta: timedelta | None = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=AUTH_ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, AUTH_SECRET_KEY, algorithm=AUTH_ALGORITHM)


def decode_token(token: str) -> dict[str, Any]:
    try:
        return jwt.decode(token, AUTH_SECRET_KEY, algorithms=[AUTH_ALGORITHM])
    except JWTError as exc:
        raise ValueError("Invalid token") from exc


class TokenData:
    """Data extracted from validated JWT token"""
    def __init__(self, owner_id: UUID, user: str, role: str | None = None):
        self.owner_id = owner_id
        self.user = user
        self.role = role


def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> TokenData:
    """
    Dependency that validates JWT token and returns authenticated user data.
    
    Usage:
        @router.get("/protected")
        def protected_route(current_user: TokenData = Depends(get_current_user)):
            return {"owner_id": current_user.owner_id}
    """
    token = credentials.credentials
    
    try:
        payload = decode_token(token)
        user = payload.get("sub")
        owner_id_str = payload.get("owner_id")
        role = payload.get("role")
        
        if user is None or owner_id_str is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authentication credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )
        
        return TokenData(
            owner_id=UUID(owner_id_str),
            user=user,
            role=role,
        )
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
