"""User management — list, create, update, deactivate."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from database import get_db
from models import User
from schemas import UserResponse
from auth import get_current_user, require_role, hash_password

router = APIRouter(prefix="/api/v1/users", tags=["users"])


class UserCreate(BaseModel):
    username: str
    email: Optional[str] = None
    password: str
    role: str = "user"  # signup default = customer tier
    display_name: Optional[str] = None
    must_change_password: bool = True  # temp passwords should be changed on first login


class UserUpdate(BaseModel):
    email: Optional[str] = None
    role: Optional[str] = None
    password: Optional[str] = None
    is_active: Optional[bool] = None
    display_name: Optional[str] = None
    must_change_password: Optional[bool] = None


@router.get("")
def list_users(db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
    users = db.query(User).order_by(User.username).all()
    result = []
    for u in users:
        r = UserResponse.model_validate(u).model_dump()
        r["display_name"] = getattr(u, "display_name", None)
        result.append(r)
    return result


VALID_ROLES = ("admin", "technician", "operator", "readonly", "user", "tenant", "agent")


@router.post("", status_code=201)
def create_user(data: UserCreate, db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
    if data.role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail="Invalid role. Use admin, technician, user, operator, readonly, tenant, or agent")
    # case-insensitive: store usernames lowercase, uniqueness is case-insensitive
    username = data.username.strip().lower()
    existing = db.query(User).filter(func.lower(User.username) == username).first()
    if existing:
        raise HTTPException(status_code=409, detail="Username already exists")

    new_user = User(
        username=username,
        email=data.email,
        hashed_password=hash_password(data.password),
        role=data.role,
        is_active=True,
        display_name=data.display_name,
        must_change_password=data.must_change_password,
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    return UserResponse.model_validate(new_user).model_dump()


@router.patch("/{user_id}")
def update_user(user_id: int, data: UserUpdate, db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    if data.role is not None:
        if data.role not in VALID_ROLES:
            raise HTTPException(status_code=400, detail="Invalid role")
        target.role = data.role
    if data.email is not None:
        target.email = data.email
    if data.password:
        target.hashed_password = hash_password(data.password)
    if data.is_active is not None:
        target.is_active = data.is_active
    if data.display_name is not None:
        target.display_name = data.display_name
    if data.must_change_password is not None:
        target.must_change_password = data.must_change_password

    db.commit()
    db.refresh(target)
    return UserResponse.model_validate(target).model_dump()


@router.delete("/{user_id}", status_code=204)
def delete_user(user_id: int, db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
    if user.id == user_id:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    _purge_user_rows(db, target)
    db.delete(target)
    db.commit()
    return None


@router.post("/{user_id}/purge")
def purge_user(user_id: int, db: Session = Depends(get_db),
               user: User = Depends(require_role("admin"))):
    """Purge one user's data (tickets, chat, sessions, ownership) while
    keeping the account itself — FK-safe, audit-logged."""
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    _purge_user_rows(db, target)
    from audit import log_event
    log_event(db, "user_data_purged", user.username,
              {"user_id": user_id, "username": target.username})
    return {"status": "ok", "purged_user_id": user_id}


def _purge_user_rows(db: Session, target: User):
    """Delete every row that references this user (FK-safe cascade), then
    clear device ownership back to unowned."""
    from models import Ticket, ChatMessage, AuthSession, Device
    db.query(Ticket).filter(Ticket.submitter_id == target.id).delete(
        synchronize_session=False)
    db.query(ChatMessage).filter(
        (ChatMessage.from_user_id == target.id)
        | (ChatMessage.to_user_id == target.id)).delete(synchronize_session=False)
    db.query(AuthSession).filter(AuthSession.user_id == target.id).delete(
        synchronize_session=False)
    db.query(Device).filter(Device.owner_id == target.id).update(
        {Device.owner_id: None}, synchronize_session=False)
    db.flush()
