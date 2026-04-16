import hashlib
import secrets

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timezone, timedelta

from app.db.session import get_db
from app.models.models import User, ApiKey, PasswordResetToken
from app.core.security import (
    verify_password, hash_password, create_access_token, get_current_user, require_admin
)
from app.core.config import settings
from app.services.user_service import seed_admin_if_empty
from app.services.email_service import send_email

router = APIRouter()


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: dict


@router.post("/login", response_model=TokenResponse)
async def login(form: OAuth2PasswordRequestForm = Depends(), db: AsyncSession = Depends(get_db)):
    await seed_admin_if_empty(db)

    result = await db.execute(select(User).where(User.email == form.username.lower()))
    user = result.scalar_one_or_none()

    if not user or not verify_password(form.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Incorrect email or password")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account disabled")

    user.last_login = datetime.utcnow()
    await db.commit()

    token = create_access_token({"sub": str(user.id), "role": str(user.role), "email": user.email})
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": _user_out(user),
    }


@router.get("/me")
async def get_me(current_user: User = Depends(get_current_user)):
    return _user_out(current_user)


@router.post("/logout")
async def logout():
    # Stateless JWT — client drops the token
    return {"message": "Logged out"}


class ForgotPasswordRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str


@router.post("/forgot-password", status_code=200)
async def forgot_password(body: ForgotPasswordRequest, db: AsyncSession = Depends(get_db)):
    """
    Initiate a password reset. Always returns 200 to prevent email enumeration.
    Sends a reset link to the user's email if the account exists.
    """
    email = body.email.lower().strip()
    result = await db.execute(select(User).where(User.email == email, User.is_active == True))
    user = result.scalar_one_or_none()

    if user:
        # Invalidate any existing unused tokens for this user
        await db.execute(
            delete(PasswordResetToken).where(
                PasswordResetToken.user_id == user.id,
                PasswordResetToken.used == False,
            )
        )

        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        expires_at = datetime.now(timezone.utc) + timedelta(hours=1)

        db.add(PasswordResetToken(
            user_id=user.id,
            token_hash=token_hash,
            expires_at=expires_at,
        ))
        await db.commit()

        # Read app_url from system_config, fall back to localhost
        from app.models.models import SystemConfig
        cfg_result = await db.execute(
            select(SystemConfig).where(SystemConfig.key == "app_url")
        )
        cfg = cfg_result.scalar_one_or_none()
        app_url = (cfg.value.rstrip("/") if cfg and cfg.value else "http://localhost:3000")

        reset_link = f"{app_url}/reset-password?token={raw_token}"
        body_text = (
            f"Hello {user.full_name or user.email},\n\n"
            f"A password reset was requested for your POD System account.\n\n"
            f"Click the link below to set a new password (expires in 1 hour):\n\n"
            f"{reset_link}\n\n"
            f"If you did not request this, you can safely ignore this email.\n\n"
            f"— POD System"
        )
        await send_email(
            db=db,
            to=user.email,
            subject="POD System — Password Reset Request",
            body=body_text,
        )

    return {"message": "If that email address is registered, you will receive a reset link shortly."}


@router.post("/reset-password", status_code=200)
async def reset_password(body: ResetPasswordRequest, db: AsyncSession = Depends(get_db)):
    """Validate a reset token and update the user's password."""
    if not body.new_password or len(body.new_password) < 8:
        raise HTTPException(status_code=422, detail="Password must be at least 8 characters")

    token_hash = hashlib.sha256(body.token.encode()).hexdigest()
    result = await db.execute(
        select(PasswordResetToken).where(PasswordResetToken.token_hash == token_hash)
    )
    reset_token = result.scalar_one_or_none()

    if not reset_token:
        raise HTTPException(status_code=400, detail="Invalid or expired reset link")
    if reset_token.used:
        raise HTTPException(status_code=400, detail="This reset link has already been used")
    if reset_token.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="This reset link has expired")

    user = await db.get(User, reset_token.user_id)
    if not user or not user.is_active:
        raise HTTPException(status_code=400, detail="Invalid or expired reset link")

    user.hashed_password = hash_password(body.new_password)
    reset_token.used = True
    await db.commit()

    return {"message": "Password updated successfully. You can now log in."}


def _user_out(u: User):
    return {
        "id": str(u.id),
        "email": u.email,
        "full_name": u.full_name,
        "role": str(u.role),
        "is_active": u.is_active,
        "last_login": u.last_login.isoformat() if u.last_login else None,
        "created_at": u.created_at.isoformat() if u.created_at else None,
    }


# ---------------------------------------------------------------------------
# API Key management (admin only)
# ---------------------------------------------------------------------------

class ApiKeyCreateRequest(BaseModel):
    name: str


@router.post("/apikeys", status_code=201)
async def create_api_key(
    body: ApiKeyCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Create a new API key. The raw key is returned once — store it securely."""
    raw_key = "pod_" + secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    key_prefix = raw_key[:12]

    api_key = ApiKey(
        key_hash=key_hash,
        key_prefix=key_prefix,
        name=body.name,
        created_by=current_user.email,
    )
    db.add(api_key)
    await db.commit()
    await db.refresh(api_key)

    return {
        "id": str(api_key.id),
        "name": api_key.name,
        "key_prefix": api_key.key_prefix,
        "created_at": api_key.created_at.isoformat() if api_key.created_at else None,
        # Returned only once:
        "api_key": raw_key,
    }


@router.get("/apikeys")
async def list_api_keys(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """List all API keys (hashes never exposed)."""
    result = await db.execute(select(ApiKey).order_by(ApiKey.created_at.desc()))
    keys = result.scalars().all()
    return [
        {
            "id": str(k.id),
            "name": k.name,
            "key_prefix": k.key_prefix,
            "is_active": k.is_active,
            "created_by": k.created_by,
            "created_at": k.created_at.isoformat() if k.created_at else None,
            "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
        }
        for k in keys
    ]


@router.delete("/apikeys/{key_id}", status_code=204)
async def revoke_api_key(
    key_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Revoke (deactivate) an API key."""
    result = await db.execute(select(ApiKey).where(ApiKey.id == key_id))
    key = result.scalar_one_or_none()
    if not key:
        raise HTTPException(status_code=404, detail="API key not found")
    key.is_active = False
    await db.commit()
