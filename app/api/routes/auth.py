from datetime import timedelta

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.db.session import SessionLocal
from app.models import Owners
from app.services.common.auth import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    create_access_token,
    hash_password,
    is_password_hashed,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    user: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str
    expires_in: int
    owner_id: str
    user: str
    role: str | None = None


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest):
    db = SessionLocal()
    try:
        owner = db.query(Owners).filter(Owners.user == payload.user).first()
        if not owner or not verify_password(payload.password, owner.password):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid credentials",
            )

        # Migrate old plaintext passwords to bcrypt at first successful login.
        if not is_password_hashed(owner.password):
            owner.password = hash_password(payload.password)
            db.commit()
            db.refresh(owner)

        expires_delta = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
        access_token = create_access_token(
            {
                "sub": owner.user,
                "owner_id": str(owner.id),
                "role": owner.role,
            },
            expires_delta=expires_delta,
        )

        return TokenResponse(
            access_token=access_token,
            token_type="bearer",
            expires_in=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
            owner_id=str(owner.id),
            user=owner.user,
            role=owner.role,
        )
    finally:
        db.close()
