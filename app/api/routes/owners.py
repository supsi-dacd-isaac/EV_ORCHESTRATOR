from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.db.session import SessionLocal
from app.models import Owners
import uuid
from datetime import datetime

router = APIRouter(prefix="/owners", tags=["owners"])


class CreateOwnerRequest(BaseModel):
    user: str
    password: str
    company_name: str


class UpdateOwnerRequest(BaseModel):
    user: str = None
    password: str = None
    company_name: str = None


@router.post("/create")
def create_owner(owner: CreateOwnerRequest):
    """
    Create a new owner in the database
    """
    db = SessionLocal()
    try:
        # Check if user already exists
        existing_owner = db.query(Owners).filter(Owners.user == owner.user).first()
        if existing_owner:
            raise HTTPException(status_code=400, detail="User already exists")

        # Create new owner
        new_owner = Owners(
            id=uuid.uuid4(),
            user=owner.user,
            password=owner.password,
            company_name=owner.company_name,
            updated_at=datetime.utcnow(),
        )
        db.add(new_owner)
        db.commit()
        db.refresh(new_owner)

        return {
            "id": str(new_owner.id),
            "user": new_owner.user,
            "company_name": new_owner.company_name,
            "updated_at": new_owner.updated_at,
        }
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@router.put("/update/{owner_uuid}")
def update_owner(owner_uuid: str, owner_data: UpdateOwnerRequest):
    """
    Update an existing owner's information
    """
    db = SessionLocal()
    try:
        # Find owner by UUID
        owner = db.query(Owners).filter(Owners.id == uuid.UUID(owner_uuid)).first()
        if not owner:
            raise HTTPException(status_code=404, detail="Owner not found")

        # Check if new user already exists (if user is being updated)
        if owner_data.user and owner_data.user != owner.user:
            existing_owner = db.query(Owners).filter(Owners.user == owner_data.user).first()
            if existing_owner:
                raise HTTPException(status_code=400, detail="User already exists")
            owner.user = owner_data.user

        # Update fields if provided
        if owner_data.password:
            owner.password = owner_data.password
        if owner_data.company_name:
            owner.company_name = owner_data.company_name

        owner.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(owner)

        return {
            "id": str(owner.id),
            "user": owner.user,
            "company_name": owner.company_name,
            "updated_at": owner.updated_at,
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()
