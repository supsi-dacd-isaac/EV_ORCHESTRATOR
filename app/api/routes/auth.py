from datetime import timedelta
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError

from app.db.session import SessionLocal
from app.models import Owners
from app.services.common.auth import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    TokenData,
    create_access_token,
    get_current_user,
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


class SignupRequest(BaseModel):
    user: str
    password: str
    company_name: str


class SignupResponse(BaseModel):
    id: str
    user: str
    company_name: str
    role: str
    type: str


class LogoutResponse(BaseModel):
    detail: str


class OwnerPublic(BaseModel):
    id: str
    user: str
    company_name: str
    type: str | None = None
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

        # Persist the active token (re-login while already logged in just replaces it)
        owner.token = access_token
        db.commit()

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


@router.post("/signup", response_model=SignupResponse, status_code=status.HTTP_201_CREATED)
def signup(payload: SignupRequest):
    db = SessionLocal()
    try:
        existing = db.query(Owners).filter(Owners.user == payload.user).first()
        if existing:
            raise HTTPException(status_code=400, detail="User already exists")

        owner = Owners(
            id=uuid4(),
            user=payload.user,
            password=hash_password(payload.password),
            company_name=payload.company_name,
            type="",
            role="guest",
        )
        db.add(owner)
        db.commit()
        db.refresh(owner)

        return SignupResponse(
            id=str(owner.id),
            user=owner.user,
            company_name=owner.company_name,
            role=owner.role or "guest",
            type=owner.type or "",
        )
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="User already exists")
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        db.close()


@router.post("/logout", response_model=LogoutResponse)
def logout(current_user: TokenData = Depends(get_current_user)):
    db = SessionLocal()
    try:
        owner = db.query(Owners).filter(Owners.user == current_user.user).first()
        if owner:
            owner.token = None
            db.commit()
    finally:
        db.close()
    return LogoutResponse(detail="Logged out successfully")


@router.get("/owners", response_model=list[OwnerPublic])
def list_owners_public():
    """Public endpoint: list all owners."""
    db = SessionLocal()
    try:
        owners = db.query(Owners).all()
        return [
            OwnerPublic(
                id=str(o.id),
                user=o.user,
                company_name=o.company_name,
                type=o.type,
                role=o.role,
            )
            for o in owners
        ]
    finally:
        db.close()
