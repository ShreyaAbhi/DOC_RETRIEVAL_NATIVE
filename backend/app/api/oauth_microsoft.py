"""
Microsoft 365 / Azure AD OAuth2 flow for monitored mailboxes.

Flow:
  1. On the setup page the user clicks "Sign in with Microsoft".
  2. Frontend calls GET /api/oauth/microsoft/start?setup_token=...
     which returns a Microsoft authorization URL (302) bearing a random `state`.
  3. Microsoft redirects the user to
     GET /api/oauth/microsoft/callback?code=...&state=...
     where we exchange the code for access + refresh tokens, persist them
     on the MonitoredEmail row, and redirect the browser to a success page.

Tokens are Fernet-encrypted at rest (same key as IMAP passwords).
"""
from __future__ import annotations

import logging
import secrets
import urllib.parse
from datetime import datetime, timedelta

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.monitored_emails import decrypt_password, encrypt_password
from app.db.session import get_db
from app.models.models import MonitoredEmail, OAuthPendingState, SystemConfig

logger = logging.getLogger(__name__)

router = APIRouter()

AUTH_URL_TMPL   = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize"
TOKEN_URL_TMPL  = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
STATE_TTL_MIN   = 15


# ── Config loader ────────────────────────────────────────────────

async def _load_ms_cfg(db: AsyncSession) -> dict:
    keys = [
        "microsoft_oauth_client_id",
        "microsoft_oauth_client_secret",
        "microsoft_oauth_tenant",
        "microsoft_oauth_scope",
        "app_base_url",
    ]
    result = await db.execute(select(SystemConfig).where(SystemConfig.key.in_(keys)))
    cfg = {r.key: (r.value or "").strip() for r in result.scalars().all()}
    cfg.setdefault("microsoft_oauth_tenant", "common")
    if not cfg.get("microsoft_oauth_tenant"):
        cfg["microsoft_oauth_tenant"] = "common"
    if not cfg.get("microsoft_oauth_scope"):
        cfg["microsoft_oauth_scope"] = (
            "offline_access https://outlook.office.com/IMAP.AccessAsUser.All"
        )
    return cfg


def _redirect_uri(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/api/oauth/microsoft/callback"


# ── Start: issue an authorize URL and redirect ──────────────────

@router.get("/start")
async def start_oauth(
    setup_token: str = Query(..., description="MonitoredEmail.setup_token"),
    db: AsyncSession = Depends(get_db),
):
    # Validate the setup token belongs to a real monitored-email row
    result = await db.execute(
        select(MonitoredEmail).where(MonitoredEmail.setup_token == setup_token)
    )
    me = result.scalar_one_or_none()
    if not me:
        raise HTTPException(404, "Invalid or expired setup link.")
    if me.token_expires_at and me.token_expires_at < datetime.now():
        raise HTTPException(410, "This setup link has expired. Please ask an admin to resend the invitation.")

    cfg = await _load_ms_cfg(db)
    client_id = cfg.get("microsoft_oauth_client_id")
    base_url  = cfg.get("app_base_url")
    if not client_id:
        raise HTTPException(
            500,
            "Microsoft OAuth is not configured. An administrator must set "
            "microsoft_oauth_client_id (and secret) in System Settings.",
        )
    if not base_url:
        raise HTTPException(
            500,
            "app_base_url is not configured — OAuth redirect URI cannot be built.",
        )

    # Clean up stale states, then create a fresh one
    await db.execute(
        OAuthPendingState.__table__.delete().where(OAuthPendingState.expires_at < datetime.now())
    )
    state = secrets.token_urlsafe(32)
    db.add(OAuthPendingState(
        state=state,
        provider="microsoft",
        setup_token=setup_token,
        monitored_email_id=str(me.id),
        expires_at=datetime.now() + timedelta(minutes=STATE_TTL_MIN),
    ))
    await db.commit()

    params = {
        "client_id":     client_id,
        "response_type": "code",
        "redirect_uri":  _redirect_uri(base_url),
        "response_mode": "query",
        "scope":         cfg["microsoft_oauth_scope"],
        "state":         state,
        "login_hint":    me.email,
        # Force the Microsoft account picker so the user actually sees
        # the Microsoft 365 sign-in window even if already signed-in.
        "prompt":        "select_account",
    }
    auth_url = AUTH_URL_TMPL.format(tenant=cfg["microsoft_oauth_tenant"]) + "?" + urllib.parse.urlencode(params)
    return RedirectResponse(auth_url, status_code=302)


# ── Callback: exchange code for tokens ──────────────────────────

@router.get("/callback")
async def oauth_callback(
    code: str | None = Query(None),
    state: str | None = Query(None),
    error: str | None = Query(None),
    error_description: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    cfg = await _load_ms_cfg(db)
    base_url = cfg.get("app_base_url", "")

    def _redirect_to_frontend(status: str, message: str = "") -> RedirectResponse:
        qs = urllib.parse.urlencode({"status": status, "message": message})
        target = f"{base_url.rstrip('/')}/setup-email/oauth-complete?{qs}" if base_url else f"/setup-email/oauth-complete?{qs}"
        return RedirectResponse(target, status_code=302)

    if error:
        logger.warning("Microsoft OAuth error: %s — %s", error, error_description)
        return _redirect_to_frontend("error", error_description or error)
    if not code or not state:
        return _redirect_to_frontend("error", "Missing code or state parameter")

    # Validate state
    pending = await db.get(OAuthPendingState, state)
    if not pending or pending.provider != "microsoft":
        return _redirect_to_frontend("error", "Invalid or unknown state parameter")
    if pending.expires_at and pending.expires_at < datetime.now():
        await db.delete(pending)
        await db.commit()
        return _redirect_to_frontend("error", "Authorization request expired, please try again")

    me = await db.get(MonitoredEmail, pending.monitored_email_id) if pending.monitored_email_id else None
    if not me:
        result = await db.execute(
            select(MonitoredEmail).where(MonitoredEmail.setup_token == pending.setup_token)
        )
        me = result.scalar_one_or_none()
    if not me:
        await db.delete(pending)
        await db.commit()
        return _redirect_to_frontend("error", "Associated mailbox no longer exists")

    # Exchange the authorization code for tokens
    token_url = TOKEN_URL_TMPL.format(tenant=cfg["microsoft_oauth_tenant"])
    data = {
        "client_id":     cfg["microsoft_oauth_client_id"],
        "scope":         cfg["microsoft_oauth_scope"],
        "code":          code,
        "redirect_uri":  _redirect_uri(base_url),
        "grant_type":    "authorization_code",
    }
    if cfg.get("microsoft_oauth_client_secret"):
        data["client_secret"] = cfg["microsoft_oauth_client_secret"]

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(token_url, data=data)
    except Exception as exc:
        logger.exception("Microsoft token exchange transport error")
        return _redirect_to_frontend("error", f"Could not reach Microsoft token endpoint: {exc}")

    if resp.status_code != 200:
        logger.warning("Microsoft token exchange failed %s: %s", resp.status_code, resp.text[:500])
        try:
            payload = resp.json()
            desc = payload.get("error_description") or payload.get("error") or resp.text
        except Exception:
            desc = resp.text
        return _redirect_to_frontend("error", f"Microsoft token exchange failed: {desc}")

    payload = resp.json()
    access_token  = payload.get("access_token")
    refresh_token = payload.get("refresh_token")
    expires_in    = int(payload.get("expires_in") or 3600)
    granted_scope = payload.get("scope") or cfg["microsoft_oauth_scope"]

    if not access_token:
        return _redirect_to_frontend("error", "Microsoft did not return an access token")
    if not refresh_token:
        # Without a refresh token we can't silently renew — user must re-auth repeatedly.
        logger.warning("Microsoft OAuth: no refresh_token returned (missing offline_access scope?)")

    # Persist on the monitored email
    me.auth_type              = "oauth_microsoft"
    me.oauth_access_token     = encrypt_password(access_token)
    me.oauth_refresh_token    = encrypt_password(refresh_token) if refresh_token else me.oauth_refresh_token
    me.oauth_token_expires_at = datetime.now() + timedelta(seconds=max(60, expires_in - 60))
    me.oauth_scope            = granted_scope
    me.imap_host              = me.imap_host or "outlook.office365.com"
    me.imap_port              = me.imap_port or 993
    me.use_ssl                = True if me.use_ssl is None else me.use_ssl
    me.mailbox_folder         = me.mailbox_folder or "INBOX"
    me.check_interval_minutes = me.check_interval_minutes or 5
    me.imap_user              = me.imap_user or me.email
    me.configured_at          = datetime.now()
    me.status                 = "active"
    me.last_error             = None
    me.last_reauth_reminder_at = None
    # Invalidate the setup token — single-use
    me.setup_token      = None
    me.token_expires_at = None

    await db.delete(pending)
    await db.commit()

    logger.info("Microsoft OAuth configured for %s (scope=%s)", me.email, granted_scope)
    return _redirect_to_frontend("success", me.email)


# ── Token refresh (used by the IMAP poller) ─────────────────────

async def refresh_microsoft_token(db: AsyncSession, me: MonitoredEmail) -> str | None:
    """
    Refresh the access token for a monitored email using its stored refresh token.
    Returns the new access token on success, or None on failure (and sets
    ``status='reauth_required'`` + ``last_error`` on the row).
    """
    if me.auth_type != "oauth_microsoft" or not me.oauth_refresh_token:
        return None

    cfg = await _load_ms_cfg(db)
    if not cfg.get("microsoft_oauth_client_id"):
        me.last_error = "Microsoft OAuth client_id is not configured"
        await db.commit()
        return None

    try:
        refresh_token = decrypt_password(me.oauth_refresh_token)
    except Exception:
        me.last_error = "Failed to decrypt stored refresh token"
        me.status = "reauth_required"
        await db.commit()
        return None

    token_url = TOKEN_URL_TMPL.format(tenant=cfg["microsoft_oauth_tenant"])
    data = {
        "client_id":     cfg["microsoft_oauth_client_id"],
        "scope":         cfg["microsoft_oauth_scope"],
        "refresh_token": refresh_token,
        "grant_type":    "refresh_token",
    }
    if cfg.get("microsoft_oauth_client_secret"):
        data["client_secret"] = cfg["microsoft_oauth_client_secret"]

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(token_url, data=data)
    except Exception as exc:
        logger.warning("MS refresh transport error for %s: %s", me.email, exc)
        me.last_error = f"Token refresh transport error: {exc}"
        await db.commit()
        return None

    if resp.status_code != 200:
        body = resp.text[:500]
        logger.warning("MS refresh failed for %s: %s %s", me.email, resp.status_code, body)
        try:
            err = resp.json().get("error", "")
        except Exception:
            err = ""
        if err in ("invalid_grant", "invalid_client", "unauthorized_client"):
            me.status = "reauth_required"
        me.last_error = f"Refresh failed: {body}"
        await db.commit()
        return None

    payload = resp.json()
    new_access  = payload.get("access_token")
    new_refresh = payload.get("refresh_token")
    expires_in  = int(payload.get("expires_in") or 3600)
    if not new_access:
        me.last_error = "Refresh response contained no access_token"
        await db.commit()
        return None

    me.oauth_access_token     = encrypt_password(new_access)
    if new_refresh:
        me.oauth_refresh_token = encrypt_password(new_refresh)
    me.oauth_token_expires_at = datetime.now() + timedelta(seconds=max(60, expires_in - 60))
    me.last_error             = None
    if me.status == "reauth_required":
        me.status = "active"
    await db.commit()
    return new_access


async def get_valid_access_token(db: AsyncSession, me: MonitoredEmail) -> str | None:
    """Return a non-expired access token for this mailbox, refreshing if needed."""
    if me.auth_type != "oauth_microsoft":
        return None
    if not me.oauth_access_token:
        return await refresh_microsoft_token(db, me)

    expires = me.oauth_token_expires_at
    if expires and expires > datetime.now() + timedelta(minutes=2):
        try:
            return decrypt_password(me.oauth_access_token)
        except Exception:
            pass
    return await refresh_microsoft_token(db, me)
