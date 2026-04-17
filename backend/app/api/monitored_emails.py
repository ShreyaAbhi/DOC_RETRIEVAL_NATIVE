from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timedelta, timezone
import secrets
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import asyncio
import base64
import hashlib

from cryptography.fernet import Fernet

from app.db.session import get_db
from app.models.models import MonitoredEmail, SystemConfig, User
from app.core.security import require_admin
from app.core.config import settings

router = APIRouter()

INVITE_EXPIRY_HOURS = 72


# ── Encryption helpers ─────────────────────────────────────────

def _get_fernet() -> Fernet:
    key = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_password(pwd: str) -> str:
    return _get_fernet().encrypt(pwd.encode()).decode()


def decrypt_password(encrypted: str) -> str:
    try:
        return _get_fernet().decrypt(encrypted.encode()).decode()
    except Exception:
        return ''


# ── SMTP helpers ───────────────────────────────────────────────

async def _get_smtp_cfg(db: AsyncSession) -> dict:
    result = await db.execute(
        select(SystemConfig).where(
            SystemConfig.key.in_(['smtp_host', 'smtp_port', 'smtp_user', 'smtp_password', 'smtp_from'])
        )
    )
    return {r.key: r.value for r in result.scalars().all()}


def _send_invite_sync(smtp_cfg: dict, to_email: str, setup_url: str):
    host     = smtp_cfg.get('smtp_host', '')
    port     = int(smtp_cfg.get('smtp_port') or 587)
    user     = smtp_cfg.get('smtp_user', '')
    pwd      = smtp_cfg.get('smtp_password', '')
    from_    = smtp_cfg.get('smtp_from') or user

    if not host or not user:
        raise ValueError("SMTP is not configured — set smtp_host, smtp_user and smtp_password in Settings.")

    msg = MIMEMultipart('alternative')
    msg['Subject'] = 'POD System — Email Account Setup Invitation'
    msg['From']    = from_
    msg['To']      = to_email

    text = (
        f"You have been invited to connect {to_email} for monitoring in the POD Automation System.\n\n"
        f"Setup link (expires in {INVITE_EXPIRY_HOURS} hours):\n{setup_url}\n\n"
        "If you did not expect this, you can safely ignore this email."
    )

    html = f"""
<html><body style="font-family:sans-serif;max-width:600px;margin:40px auto;color:#333">
  <h2 style="color:#0d6efd">POD Automation System</h2>
  <p>You have been invited to connect <strong>{to_email}</strong> for email monitoring.</p>
  <p>Click the button below to configure your mailbox settings.
     This link expires in <strong>{INVITE_EXPIRY_HOURS} hours</strong>.</p>
  <p style="margin:30px 0">
    <a href="{setup_url}"
       style="background:#0d6efd;color:white;padding:12px 28px;border-radius:6px;
              text-decoration:none;font-weight:bold;font-size:15px">
      Set Up Email Account
    </a>
  </p>
  <p style="color:#666;font-size:13px">
    Or copy this link into your browser:<br>
    <code style="background:#f5f5f5;padding:4px 8px;border-radius:4px">{setup_url}</code>
  </p>
  <hr style="border:none;border-top:1px solid #eee;margin:30px 0"/>
  <p style="color:#999;font-size:12px">
    If you did not expect this invitation, you can safely ignore this email.
  </p>
</body></html>
"""

    msg.attach(MIMEText(text, 'plain'))
    msg.attach(MIMEText(html, 'html'))

    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx) as srv:
            srv.login(user, pwd)
            srv.send_message(msg)
    else:
        with smtplib.SMTP(host, port) as srv:
            srv.ehlo()
            srv.starttls(context=ctx)
            srv.login(user, pwd)
            srv.send_message(msg)


async def _send_invite_async(smtp_cfg: dict, to_email: str, setup_url: str):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _send_invite_sync, smtp_cfg, to_email, setup_url)


# ── Background invite sender ───────────────────────────────────

async def _send_invite_bg(me_id: str, token: str):
    from app.db.session import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        me = await db.get(MonitoredEmail, me_id)
        if not me:
            return
        smtp_cfg = await _get_smtp_cfg(db)

        # Get app_base_url
        row = await db.get(SystemConfig, 'app_base_url')
        base_url = (row.value or '').strip().rstrip('/') if row else ''
        setup_url = f"{base_url}/setup-email?token={token}"

        try:
            await _send_invite_async(smtp_cfg, me.email, setup_url)
        except Exception as exc:
            me.last_error = f"Invite email failed: {exc}"
            await db.commit()


# ── Schemas ────────────────────────────────────────────────────

class AddEmailBody(BaseModel):
    email: str
    display_name: Optional[str] = None
    notes: Optional[str] = None


class SetupBody(BaseModel):
    imap_host: str
    imap_port: int = 993
    imap_user: str
    imap_password: str
    use_ssl: bool = True
    mailbox_folder: str = 'INBOX'
    check_interval_minutes: int = 5


# ── Admin endpoints ────────────────────────────────────────────

class QuickInviteBody(BaseModel):
    email: str
    subject: Optional[str] = None
    custom_message: Optional[str] = None


@router.post("/quick-invite")
async def quick_invite(
    body: QuickInviteBody,
    bg: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Create (or refresh) a monitored email entry and send a fully-composed invitation."""
    normalized = body.email.lower().strip()

    # Upsert
    result = await db.execute(select(MonitoredEmail).where(MonitoredEmail.email == normalized))
    me = result.scalar_one_or_none()
    if me:
        me.setup_token      = secrets.token_urlsafe(32)
        me.token_expires_at = datetime.utcnow() + timedelta(hours=INVITE_EXPIRY_HOURS)
        if me.status not in ('active',):
            me.status = 'pending'
    else:
        me = MonitoredEmail(
            email=normalized,
            setup_token=secrets.token_urlsafe(32),
            token_expires_at=datetime.utcnow() + timedelta(hours=INVITE_EXPIRY_HOURS),
            created_by=admin.email,
        )
        db.add(me)

    await db.commit()
    await db.refresh(me)

    token   = me.setup_token
    me_id   = str(me.id)
    subject = body.subject or "You're invited to connect your email for POD monitoring"

    bg.add_task(_send_quick_invite_bg, me_id, token, admin.email, admin.full_name or admin.email, subject, body.custom_message or '')
    return _out(me)


async def _send_quick_invite_bg(me_id: str, token: str, admin_email: str, admin_name: str, subject: str, custom_msg: str):
    from app.db.session import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        me = await db.get(MonitoredEmail, me_id)
        if not me:
            return
        smtp_cfg = await _get_smtp_cfg(db)

        row = await db.get(SystemConfig, 'app_base_url')
        base_url  = (row.value or '').strip().rstrip('/') if row else ''
        setup_url = f"{base_url}/setup-email?token={token}"

        try:
            await _send_quick_invite_async(smtp_cfg, me.email, setup_url, admin_email, admin_name, subject, custom_msg)
        except Exception as exc:
            me.last_error = f"Quick invite failed: {exc}"
            await db.commit()


def _send_quick_invite_sync(smtp_cfg: dict, to_email: str, setup_url: str,
                             admin_email: str, admin_name: str, subject: str, custom_msg: str):
    host     = smtp_cfg.get('smtp_host', '')
    port     = int(smtp_cfg.get('smtp_port') or 587)
    user     = smtp_cfg.get('smtp_user', '')
    pwd      = smtp_cfg.get('smtp_password', '')
    from_    = smtp_cfg.get('smtp_from') or user

    if not host or not user:
        raise ValueError("SMTP not configured — set smtp_host, smtp_user and smtp_password in Settings.")

    custom_block_text = f"\n{custom_msg}\n" if custom_msg else ""
    custom_block_html = f'<p style="color:#333">{custom_msg}</p>' if custom_msg else ""

    text = f"""Hi,

{admin_name} ({admin_email}) has invited you to connect your email account to the POD Automation System.
{custom_block_text}
The system will monitor this mailbox for incoming Proof-of-Delivery requests and process them automatically.

──────────────────────────────────────────
  SET UP YOUR EMAIL ACCOUNT
  {setup_url}
──────────────────────────────────────────

This link expires in {INVITE_EXPIRY_HOURS} hours. You will be asked to enter your IMAP server settings
(host, port, and credentials). We recommend using an app-specific password rather than your main account password.

Steps:
  1. Click the link above
  2. Enter your IMAP server details
  3. Click "Connect Email Account"

Once configured, no further action is needed — the system will handle incoming emails automatically.

If you did not expect this invitation, you can safely ignore this message.

— Sent via POD Automation System on behalf of {admin_name}
""".strip()

    html = f"""
<html><body style="font-family:Arial,sans-serif;max-width:620px;margin:40px auto;color:#333;font-size:15px;line-height:1.6">
  <div style="background:#f8f9fa;border-radius:8px;padding:32px">
    <h2 style="color:#0d6efd;margin-top:0">POD Automation System</h2>
    <p><strong>{admin_name}</strong> (<a href="mailto:{admin_email}">{admin_email}</a>) has invited you to connect
       <strong>{to_email}</strong> for email monitoring.</p>
    {custom_block_html}
    <p>The system will automatically monitor this mailbox for incoming Proof-of-Delivery requests.</p>

    <div style="text-align:center;margin:32px 0">
      <a href="{setup_url}"
         style="background:#0d6efd;color:#fff;padding:14px 32px;border-radius:6px;
                text-decoration:none;font-weight:bold;font-size:16px;display:inline-block">
        Set Up My Email Account
      </a>
    </div>

    <p style="font-size:13px;color:#666">
      This link expires in <strong>{INVITE_EXPIRY_HOURS} hours</strong>. You will be asked to provide your
      IMAP server settings. We recommend using an <strong>app-specific password</strong>.
    </p>

    <div style="background:#fff;border:1px solid #dee2e6;border-radius:6px;padding:16px;margin:20px 0;font-size:13px;color:#555">
      <strong>Steps to complete setup:</strong>
      <ol style="margin:8px 0 0 0;padding-left:20px">
        <li>Click the button above</li>
        <li>Enter your IMAP server details (host, port, credentials)</li>
        <li>Click <em>Connect Email Account</em></li>
      </ol>
    </div>

    <p style="font-size:13px;color:#666">
      Can't click the button? Copy this link into your browser:<br>
      <code style="background:#f5f5f5;padding:3px 6px;border-radius:4px;font-size:12px;word-break:break-all">{setup_url}</code>
    </p>

    <hr style="border:none;border-top:1px solid #dee2e6;margin:24px 0"/>
    <p style="font-size:12px;color:#aaa;margin:0">
      Sent via POD Automation System on behalf of {admin_name} · If you did not expect this, ignore this message.
    </p>
  </div>
</body></html>
""".strip()

    msg = MIMEMultipart('alternative')
    msg['Subject']  = subject
    msg['From']     = from_
    msg['To']       = to_email
    msg['Reply-To'] = admin_email
    msg.attach(MIMEText(text, 'plain'))
    msg.attach(MIMEText(html, 'html'))

    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx) as srv:
            srv.login(user, pwd)
            srv.send_message(msg)
    else:
        with smtplib.SMTP(host, port) as srv:
            srv.ehlo()
            srv.starttls(context=ctx)
            srv.login(user, pwd)
            srv.send_message(msg)


async def _send_quick_invite_async(smtp_cfg, to_email, setup_url, admin_email, admin_name, subject, custom_msg):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _send_quick_invite_sync,
                               smtp_cfg, to_email, setup_url, admin_email, admin_name, subject, custom_msg)


@router.post("/poll-now")
async def force_poll_now(
    bg: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """
    Immediately trigger an IMAP poll for all active monitored email accounts.
    Runs in a background task so the response returns instantly.
    """
    from app.services.imap_service import poll_monitored_email
    from app.db.session import AsyncSessionLocal

    result = await db.execute(
        select(MonitoredEmail).where(MonitoredEmail.status != 'disabled')
    )
    active = result.scalars().all()
    if not active:
        return {"triggered": 0, "message": "No active monitored email accounts"}

    me_ids = [str(m.id) for m in active]

    async def _run_polls():
        import logging
        log = logging.getLogger(__name__)
        async with AsyncSessionLocal() as session:
            for me_id in me_ids:
                try:
                    me = await session.get(MonitoredEmail, me_id)
                    if me:
                        count = await poll_monitored_email(session, me)
                        log.info("Force poll: %s ingested %d email(s)", me.email, count)
                except Exception as exc:
                    log.error("Force poll error for %s: %s", me_id, exc)

    bg.add_task(_run_polls)
    return {"triggered": len(active), "message": f"Polling {len(active)} mailbox(es) now"}


@router.get("")
async def list_monitored_emails(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    result = await db.execute(
        select(MonitoredEmail).order_by(MonitoredEmail.created_at.desc())
    )
    return [_out(m) for m in result.scalars().all()]


@router.post("")
async def add_monitored_email(
    body: AddEmailBody,
    bg: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    normalized = body.email.lower().strip()
    existing = await db.execute(
        select(MonitoredEmail).where(MonitoredEmail.email == normalized)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(400, "This email address is already registered for monitoring.")

    token   = secrets.token_urlsafe(32)
    expires = datetime.utcnow() + timedelta(hours=INVITE_EXPIRY_HOURS)

    me = MonitoredEmail(
        email=normalized,
        display_name=body.display_name,
        notes=body.notes,
        setup_token=token,
        token_expires_at=expires,
        created_by=admin.email,
    )
    db.add(me)
    await db.commit()
    await db.refresh(me)

    bg.add_task(_send_invite_bg, str(me.id), token)
    return _out(me)


@router.delete("/{me_id}")
async def remove_monitored_email(
    me_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    me = await db.get(MonitoredEmail, me_id)
    if not me:
        raise HTTPException(404, "Not found")
    await db.delete(me)
    await db.commit()
    return {"ok": True}


@router.post("/{me_id}/resend-invite")
async def resend_invite(
    me_id: str,
    bg: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    me = await db.get(MonitoredEmail, me_id)
    if not me:
        raise HTTPException(404, "Not found")

    me.setup_token      = secrets.token_urlsafe(32)
    me.token_expires_at = datetime.utcnow() + timedelta(hours=INVITE_EXPIRY_HOURS)
    if me.status not in ('active',):
        me.status = 'pending'
    await db.commit()
    await db.refresh(me)

    bg.add_task(_send_invite_bg, str(me.id), me.setup_token)
    return {"ok": True, "message": f"Invite resent to {me.email}"}


@router.get("/{me_id}/invite-link")
async def get_invite_link(
    me_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    me = await db.get(MonitoredEmail, me_id)
    if not me:
        raise HTTPException(404, "Not found")
    if not me.setup_token:
        # Generate a fresh token if none exists
        me.setup_token      = secrets.token_urlsafe(32)
        me.token_expires_at = datetime.utcnow() + timedelta(hours=INVITE_EXPIRY_HOURS)
        await db.commit()
        await db.refresh(me)

    row = await db.get(SystemConfig, 'app_base_url')
    base_url = (row.value or '').strip().rstrip('/') if row else ''
    url = f"{base_url}/setup-email?token={me.setup_token}"
    return {
        "url":              url,
        "token":            me.setup_token,
        "token_expires_at": me.token_expires_at.isoformat() if me.token_expires_at else None,
        "base_url_configured": bool(base_url),
    }


@router.post("/{me_id}/send-invite-email")
async def send_invite_email_now(
    me_id: str,
    bg: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    me = await db.get(MonitoredEmail, me_id)
    if not me:
        raise HTTPException(404, "Not found")
    if not me.setup_token:
        me.setup_token      = secrets.token_urlsafe(32)
        me.token_expires_at = datetime.utcnow() + timedelta(hours=INVITE_EXPIRY_HOURS)
        await db.commit()
        await db.refresh(me)
    bg.add_task(_send_invite_bg, str(me.id), me.setup_token)
    return {"ok": True, "message": f"Invite email queued for {me.email}"}


@router.put("/{me_id}/toggle")
async def toggle_monitored_email(
    me_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    me = await db.get(MonitoredEmail, me_id)
    if not me:
        raise HTTPException(404, "Not found")
    me.status = 'disabled' if me.status == 'active' else 'active'
    await db.commit()
    return _out(me)


# ── Public setup endpoints (token-based, no auth required) ─────

@router.get("/setup/{token}")
async def get_setup_info(token: str, db: AsyncSession = Depends(get_db)):
    me = await _resolve_token(token, db)

    # Is Microsoft OAuth configured on the server? If yes we'll surface the
    # "Sign in with Microsoft" button on the setup page.
    ms_row = await db.get(SystemConfig, 'microsoft_oauth_client_id')
    microsoft_oauth_enabled = bool((ms_row.value or '').strip()) if ms_row else False

    return {
        "email":        me.email,
        "display_name": me.display_name,
        "configured":   me.configured_at is not None,
        "imap_host":    me.imap_host or '',
        "imap_port":    me.imap_port or 993,
        "imap_user":    me.imap_user or me.email,
        "use_ssl":      me.use_ssl if me.use_ssl is not None else True,
        "mailbox_folder":          me.mailbox_folder or 'INBOX',
        "check_interval_minutes":  me.check_interval_minutes or 5,
        "auth_type":               me.auth_type or 'password',
        "microsoft_oauth_enabled": microsoft_oauth_enabled,
        "is_reauth":               me.status == 'reauth_required',
    }


def _test_imap_connection(imap_host: str, imap_port: int, imap_user: str,
                           imap_password: str, use_ssl: bool, mailbox_folder: str) -> str | None:
    """
    Attempt a real IMAP login and SELECT the mailbox.
    Returns None on success, or an error message string on failure.
    Detects Microsoft 365 BasicAuthBlocked errors and returns a specific hint.
    """
    import imaplib as _imap
    try:
        conn = _imap.IMAP4_SSL(imap_host, imap_port) if use_ssl else _imap.IMAP4(imap_host, imap_port)
        conn.login(imap_user, imap_password)
        status, _ = conn.select(mailbox_folder)
        conn.logout()
        if status != "OK":
            return f"Could not select mailbox '{mailbox_folder}'"
        return None
    except _imap.IMAP4.error as e:
        err_str = str(e)
        if "BasicAuthBlocked" in err_str or "LogonDenied" in err_str:
            return (
                "BASIC_AUTH_BLOCKED: Microsoft 365 has disabled password-based "
                "authentication for this account. Please use the 'Sign in with "
                "Microsoft' option instead, which uses secure OAuth2 authentication."
            )
        return f"IMAP authentication failed: {e}"
    except OSError as e:
        return f"Could not connect to {imap_host}:{imap_port} — {e}"
    except Exception as e:
        return f"Connection error: {e}"


@router.post("/setup/{token}")
async def complete_setup(token: str, body: SetupBody, db: AsyncSession = Depends(get_db)):
    me = await _resolve_token(token, db)

    # Test the IMAP connection before saving anything
    loop = asyncio.get_event_loop()
    error = await loop.run_in_executor(
        None,
        _test_imap_connection,
        body.imap_host, body.imap_port, body.imap_user,
        body.imap_password, body.use_ssl, body.mailbox_folder,
    )
    if error:
        # Always return detail as a plain string so toast.error() never
        # receives an object (which would crash React rendering).
        if error.startswith("BASIC_AUTH_BLOCKED:"):
            raise HTTPException(
                status_code=400,
                detail="BASIC_AUTH_BLOCKED: " + error.split(":", 1)[1].strip(),
            )
        raise HTTPException(status_code=400, detail=f"Connection test failed: {error}")

    me.imap_host              = body.imap_host
    me.imap_port              = body.imap_port
    me.imap_user              = body.imap_user
    me.imap_password          = encrypt_password(body.imap_password)
    me.use_ssl                = body.use_ssl
    me.mailbox_folder         = body.mailbox_folder
    me.check_interval_minutes = body.check_interval_minutes
    me.configured_at          = datetime.utcnow()
    me.status                 = 'active'
    # Invalidate token — single-use
    me.setup_token      = None
    me.token_expires_at = None

    await db.commit()
    return {"ok": True, "email": me.email}


# ── Helpers ────────────────────────────────────────────────────

async def _resolve_token(token: str, db: AsyncSession) -> MonitoredEmail:
    result = await db.execute(
        select(MonitoredEmail).where(MonitoredEmail.setup_token == token)
    )
    me = result.scalar_one_or_none()
    if not me:
        raise HTTPException(404, "Invalid or expired setup link.")
    if me.token_expires_at:
        # Compare as naive UTC – SQLite strips tzinfo on round-trip
        expires = me.token_expires_at.replace(tzinfo=None)
        if expires < datetime.utcnow():
            raise HTTPException(410, "This setup link has expired. Please ask an admin to resend the invitation.")
    return me


def _out(m: MonitoredEmail) -> dict:
    return {
        "id":                      str(m.id),
        "email":                   m.email,
        "display_name":            m.display_name,
        "status":                  m.status,
        "notes":                   m.notes,
        "imap_host":               m.imap_host,
        "imap_port":               m.imap_port,
        "imap_user":               m.imap_user,
        "use_ssl":                 m.use_ssl,
        "mailbox_folder":          m.mailbox_folder,
        "check_interval_minutes":  m.check_interval_minutes,
        "configured_at":           m.configured_at.isoformat() if m.configured_at else None,
        "created_at":              m.created_at.isoformat() if m.created_at else None,
        "created_by":              m.created_by,
        "last_checked_at":         m.last_checked_at.isoformat() if m.last_checked_at else None,
        "last_error":              m.last_error,
        "auth_type":               m.auth_type or 'password',
        "oauth_token_expires_at":  m.oauth_token_expires_at.isoformat() if m.oauth_token_expires_at else None,
        "has_pending_invite":      m.setup_token is not None,
        "token_expires_at":        m.token_expires_at.isoformat() if m.token_expires_at else None,
    }
