import base64
import json
import logging
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from pydantic import BaseModel

from app.services.email_service import send_email

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.exceptions import InvalidSignature

from app.db.session import get_db
from app.models.models import User, SystemConfig
from app.core.security import require_admin, require_super_admin

logger = logging.getLogger(__name__)
router = APIRouter()

# Public key embedded in the release — customers cannot forge keys without the private key
_PUBLIC_KEY_PEM = b"""-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAwnv6fRQfOw/ZtdL1CijL
htLYXuNiLFF1884t7H715xgZiFTtVbfZhwbSrT1kify5PVGiniUD1kRz8HGkKkuq
kxdGGrvUXa4Uf6Auhrj/jlTDpo8C77wbYq0zjEeLcqR9R59kLLwdmL9MS3NxsGbd
7seN9z7T5Jjq09AK6IaI+h58OBZ9jP9b6QimVcwwz+r8QBFwmy0z9Y/UYwSvmVkz
s+oWpybLm35IylS0L6F9ljqRu50SKf1nQkOsiRm6BGIAg/Hk39PN29ZQE/p223pm
6GHBLhao+QeouPjWQUfuAL5Em4lg1s6LyYJ8qtxxab0NVYDRrZcIcLhcEhcbAQu/
2wIDAQAB
-----END PUBLIC KEY-----"""

_public_key = serialization.load_pem_public_key(_PUBLIC_KEY_PEM)


# ── Crypto helpers ─────────────────────────────────────────────

def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    pad = 4 - len(s) % 4
    return base64.urlsafe_b64decode(s + "=" * pad)


def _verify_key(key: str) -> dict | None:
    """
    Verify an RSA-signed license key.
    Format: <base64url(json_payload)>.<base64url(RSA-SHA256 signature)>
    Payload JSON: {"to": "...", "exp": "YYYY-MM-DD", "users": N}
    Returns decoded payload dict on success, None on failure.
    """
    parts = key.strip().split(".")
    if len(parts) != 2:
        return None
    payload_b64, sig_b64 = parts
    try:
        sig = _b64url_decode(sig_b64)
        _public_key.verify(sig, payload_b64.encode(), padding.PKCS1v15(), hashes.SHA256())
        payload_bytes = _b64url_decode(payload_b64)
        return json.loads(payload_bytes)
    except (InvalidSignature, Exception):
        return None


def _build_status(payload: dict, current_users: int) -> dict:
    licensed_to = payload.get("to", "")
    expiry_str = payload.get("exp", "")
    max_users = int(payload.get("users", 0))

    expired = False
    days_remaining = None
    if expiry_str:
        try:
            expiry_date = date.fromisoformat(expiry_str)
            days_remaining = (expiry_date - date.today()).days
            expired = days_remaining < 0
        except ValueError:
            pass

    return {
        "valid": True,
        "licensed_to": licensed_to,
        "expiry": expiry_str,
        "max_users": max_users,
        "current_users": current_users,
        "days_remaining": days_remaining,
        "expired": expired,
    }


async def _load_license_key(db: AsyncSession) -> str | None:
    row = await db.execute(select(SystemConfig).where(SystemConfig.key == "license_key"))
    cfg = row.scalar_one_or_none()
    return cfg.value if cfg and cfg.value else None


async def _count_active_users(db: AsyncSession) -> int:
    result = await db.execute(
        select(func.count()).select_from(User).where(User.is_active == True)
    )
    return result.scalar()


# ── Endpoints ─────────────────────────────────────────────────

@router.post("/validate")
async def validate_license(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Verify the license key stored in system_config using the embedded RSA public key."""
    key = await _load_license_key(db)
    if not key:
        raise HTTPException(400, "No license key configured")

    payload = _verify_key(key)
    if payload is None:
        return {"valid": False, "error": "License key signature is invalid"}

    current_users = await _count_active_users(db)
    return _build_status(payload, current_users)


@router.get("/status")
async def license_status(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Return current license status (safe for page-load polling)."""
    key = await _load_license_key(db)
    if not key:
        return {"valid": False, "reason": "no_key"}

    payload = _verify_key(key)
    if payload is None:
        return {"valid": False, "reason": "invalid_signature"}

    current_users = await _count_active_users(db)
    return _build_status(payload, current_users)


class GenerateRequest(BaseModel):
    licensed_to: str
    expiry: str
    max_users: int
    client_email: str
    temp_password: str
    site_url: str


@router.post("/generate")
async def generate_license(
    body: GenerateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_super_admin),
):
    """Generate a signed license key and email it to the client. Super admin only."""

    if not body.licensed_to.strip():
        raise HTTPException(400, "licensed_to is required")
    if not body.client_email.strip():
        raise HTTPException(400, "client_email is required")
    if not body.temp_password.strip():
        raise HTTPException(400, "temp_password is required")
    if not body.site_url.strip():
        raise HTTPException(400, "site_url is required")
    try:
        expiry_date = date.fromisoformat(body.expiry)
    except ValueError:
        raise HTTPException(400, "expiry must be YYYY-MM-DD")
    if body.max_users < 1:
        raise HTTPException(400, "max_users must be at least 1")

    # Load private key from disk (only present on vendor machine / vendor deployment)
    try:
        with open("/app/scripts/private.pem", "rb") as f:
            private_key = serialization.load_pem_private_key(f.read(), password=None)
    except FileNotFoundError:
        raise HTTPException(500, "Private key not available on this server")

    payload = json.dumps(
        {"to": body.licensed_to.strip(), "exp": body.expiry, "users": body.max_users},
        separators=(",", ":"),
    )
    payload_b64 = _b64url_encode(payload.encode())
    sig = private_key.sign(payload_b64.encode(), padding.PKCS1v15(), hashes.SHA256())
    key = f"{payload_b64}.{_b64url_encode(sig)}"
    days = (expiry_date - date.today()).days
    site = body.site_url.rstrip("/")

    email_body = (
        f"<p>Hello,</p>"
        f"<p>Your <strong>POD System</strong> license for <strong>{body.licensed_to.strip()}</strong> is ready.</p>"
        f"<table style='border-collapse:collapse;font-family:monospace;font-size:13px;margin:16px 0'>"
        f"<tr><td style='padding:4px 12px 4px 0;color:#666;white-space:nowrap'>System URL</td>"
        f"<td><a href='{site}'>{site}</a></td></tr>"
        f"<tr><td style='padding:4px 12px 4px 0;color:#666'>Login Email</td><td><strong>{body.client_email.strip()}</strong></td></tr>"
        f"<tr><td style='padding:4px 12px 4px 0;color:#666'>Temporary Password</td><td><strong>{body.temp_password}</strong></td></tr>"
        f"<tr><td style='padding:4px 12px 4px 0;color:#666'>License Key</td>"
        f"<td style='word-break:break-all;max-width:480px'><code>{key}</code></td></tr>"
        f"<tr><td style='padding:4px 12px 4px 0;color:#666'>Expires</td><td>{body.expiry} ({days} days)</td></tr>"
        f"<tr><td style='padding:4px 12px 4px 0;color:#666'>Max Users</td><td>{body.max_users}</td></tr>"
        f"</table>"
        f"<p><strong>Getting started:</strong></p>"
        f"<ol>"
        f"<li>Visit <a href='{site}'>{site}</a></li>"
        f"<li>You will be prompted to enter your license key and set up your admin account.</li>"
        f"<li>Enter the license key above along with your login email and temporary password.</li>"
        f"<li>After logging in you will be asked to set a new password.</li>"
        f"</ol>"
        f"<p style='color:#888;font-size:12px'>Keep this email — the license key cannot be recovered if lost.</p>"
    )

    email_result = await send_email(
        db=db,
        to=body.client_email.strip(),
        subject=f"POD System License — {body.licensed_to.strip()}",
        body=email_body,
    )

    return {
        "key": key,
        "licensed_to": body.licensed_to.strip(),
        "expiry": body.expiry,
        "max_users": body.max_users,
        "days_valid": days,
        "email_sent": email_result.get("sent", False),
        "email_error": email_result.get("error"),
    }
