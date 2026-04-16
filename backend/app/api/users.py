from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from pydantic import BaseModel, EmailStr
from typing import Optional
from datetime import datetime

from app.db.session import get_db
from app.models.models import User, SystemConfig
from app.core.security import require_admin, require_super_admin, get_current_user, hash_password
from app.core.config import settings

router = APIRouter()


class UserCreate(BaseModel):
    email: str
    full_name: Optional[str] = None
    password: str
    role: str = "reviewer"  # "admin" | "reviewer"


class UserRoleUpdate(BaseModel):
    role: str  # "admin" | "reviewer"


class UserPasswordUpdate(BaseModel):
    password: str


@router.get("")
async def list_users(
    limit: int = 200,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    result = await db.execute(select(User).order_by(User.created_at).limit(limit).offset(offset))
    return [_out(u) for u in result.scalars().all()]


@router.post("")
async def create_user(
    data: UserCreate,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    if data.role not in ("admin", "reviewer"):
        raise HTTPException(400, "Role must be 'admin' or 'reviewer'")
    if data.email.lower() == settings.VENDOR_EMAIL.lower():
        raise HTTPException(400, "Cannot create a user with the vendor email address")

    existing = await db.execute(select(User).where(User.email == data.email.lower()))
    if existing.scalar_one_or_none():
        raise HTTPException(400, f"User with email {data.email} already exists")

    # Enforce license_max_users if set
    lic_row = await db.execute(select(SystemConfig).where(SystemConfig.key == "license_max_users"))
    lic_cfg = lic_row.scalar_one_or_none()
    if lic_cfg and lic_cfg.value:
        try:
            max_users = int(lic_cfg.value)
            if max_users > 0:
                count_result = await db.execute(
                    select(func.count()).select_from(User).where(User.is_active == True)
                )
                current = count_result.scalar()
                if current >= max_users:
                    raise HTTPException(
                        403,
                        f"License limit reached: maximum {max_users} active users allowed"
                    )
        except ValueError:
            pass

    user = User(
        email=data.email.lower(),
        full_name=data.full_name,
        hashed_password=hash_password(data.password),
        role=data.role,
        created_by=admin.email,
    )
    db.add(user)
    await db.commit()
    return _out(user)


@router.put("/{user_id}/role")
async def update_role(
    user_id: str,
    data: UserRoleUpdate,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    if data.role not in ("admin", "reviewer"):
        raise HTTPException(400, "Role must be 'admin' or 'reviewer'")

    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(404)
    if str(user.role) == "super_admin":
        raise HTTPException(403, "Cannot change the role of the super admin account")

    user.role = data.role
    await db.commit()
    return _out(user)


@router.put("/{user_id}/password")
async def update_password(
    user_id: str,
    data: UserPasswordUpdate,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(404)
    user.hashed_password = hash_password(data.password)
    await db.commit()
    return {"message": "Password updated"}


@router.delete("/{user_id}")
async def deactivate_user(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(404)
    if str(user.id) == str(admin.id):
        raise HTTPException(400, "Cannot deactivate your own account")
    if str(user.role) == "super_admin":
        raise HTTPException(403, "Cannot deactivate the super admin account")
    user.is_active = False
    await db.commit()
    return {"deactivated": True}


@router.put("/{user_id}/activate")
async def activate_user(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(404)
    user.is_active = True
    await db.commit()
    return _out(user)


def _out(u: User):
    return {
        "id": str(u.id),
        "email": u.email,
        "full_name": u.full_name,
        "role": str(u.role),
        "is_active": u.is_active,
        "created_by": u.created_by,
        "last_login": u.last_login.isoformat() if u.last_login else None,
        "created_at": u.created_at.isoformat() if u.created_at else None,
    }
