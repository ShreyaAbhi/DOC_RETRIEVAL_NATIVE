from app.core.celery_app import celery_app
import logging
import os

logger = logging.getLogger(__name__)


def _make_session_factory():
    """Create a fresh engine + session factory with NullPool for each task.
    This avoids asyncio event-loop conflicts when Celery reuses worker processes.
    """
    from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
    from sqlalchemy.pool import NullPool
    from app.core.config import settings
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    return engine, async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@celery_app.task(
    name="process_email",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    acks_late=True,
)
def process_email_task(self, request_id: str):
    import asyncio
    from app.agents.pipeline import process_email_request

    async def run():
        engine, SessionLocal = _make_session_factory()
        try:
            async with SessionLocal() as db:
                await process_email_request(db, request_id)
                await db.commit()
        finally:
            await engine.dispose()

    async def mark_failed(exc):
        from app.models.models import EmailRequest
        from app.services.audit_service import log_audit
        engine, SessionLocal = _make_session_factory()
        try:
            async with SessionLocal() as db:
                req = await db.get(EmailRequest, request_id)
                if req:
                    req.status = "failed"
                    req.error_message = f"Pipeline failed after {self.max_retries} retries: {exc}"
                    await log_audit(db, request_id, "error",
                                    f"Pipeline failed after all retries: {exc}",
                                    {"error": str(exc), "retries": self.max_retries},
                                    success=False)
                    await db.commit()
        finally:
            await engine.dispose()

    try:
        asyncio.run(run())
    except Exception as exc:
        logger.error("Pipeline task failed for %s (attempt %d): %s",
                     request_id, self.request.retries + 1, exc)
        try:
            raise self.retry(exc=exc)
        except self.MaxRetriesExceededError:
            logger.error("Pipeline max retries exceeded for %s", request_id)
            asyncio.run(mark_failed(exc))


@celery_app.task(
    name="resume_pod",
    bind=True,
    max_retries=2,
    default_retry_delay=15,
    acks_late=True,
)
def resume_pod_task(self, request_id: str):
    """Resume a pipeline that was waiting for a carrier POD reply."""
    import asyncio
    from app.agents.pipeline import resume_after_pod_received

    async def run():
        engine, SessionLocal = _make_session_factory()
        try:
            async with SessionLocal() as db:
                await resume_after_pod_received(db, request_id)
                await db.commit()
        finally:
            await engine.dispose()

    try:
        asyncio.run(run())
    except Exception as exc:
        logger.error("resume_pod_task failed for %s: %s", request_id, exc)
        try:
            raise self.retry(exc=exc)
        except self.MaxRetriesExceededError:
            logger.error("resume_pod_task max retries for %s", request_id)


@celery_app.task(name="app.core.tasks.scan_order_documents_task")
def scan_order_documents_task(order_ids: list):
    """
    Scan packing_slips and invoices folders for newly autopoll-imported orders.
    Reuses the pipeline's folder-scan helpers (_find_packing_slip_in_db,
    _find_invoice_in_db) which save matches to DB on first discovery.
    """
    import asyncio
    from app.agents.pipeline import _find_packing_slip_in_db, _find_invoice_in_db
    from app.services.pod_folder_service import scan_pod_folder_for_order
    from app.models.models import Order

    async def run():
        engine, SessionLocal = _make_session_factory()
        try:
            async with SessionLocal() as db:
                for order_id in order_ids:
                    try:
                        order = await db.get(Order, order_id)
                        if order:
                            await scan_pod_folder_for_order(db, order)
                            await _find_packing_slip_in_db(db, order)
                            await _find_invoice_in_db(db, order)
                            await db.commit()
                    except Exception as e:
                        logger.warning("scan_order_documents_task: error for order %s: %s", order_id, e)
        finally:
            await engine.dispose()

    asyncio.run(run())


@celery_app.task(name="app.core.tasks.trigger_power_automate_task")
def trigger_power_automate_task(order_ids: list):
    """
    Fire Power Automate webhook(s) for newly created orders if webhook URLs
    are configured in system_config.  Runs in addition to (not instead of)
    folder and FTP document retrieval.  Silently skips if both URLs are blank.
    """
    import asyncio
    from sqlalchemy import select
    from app.models.models import Order, SystemConfig
    from app.services.power_automate_service import trigger_for_order

    async def run():
        engine, SessionLocal = _make_session_factory()
        try:
            async with SessionLocal() as db:
                result = await db.execute(
                    select(SystemConfig).where(SystemConfig.key.in_([
                        "power_automate_packing_slip_url",
                        "power_automate_invoice_url",
                    ]))
                )
                cfg = {r.key: r.value for r in result.scalars().all()}
                ps_url  = cfg.get("power_automate_packing_slip_url") or None
                inv_url = cfg.get("power_automate_invoice_url") or None

                if not ps_url and not inv_url:
                    logger.debug("trigger_power_automate_task: no webhook URLs configured, skipping")
                    return

                for order_id in order_ids:
                    order = await db.get(Order, order_id)
                    if order:
                        await trigger_for_order(order, ps_url, inv_url)
        finally:
            await engine.dispose()

    asyncio.run(run())


@celery_app.task(name="app.core.tasks.preread_documents_task")
def preread_documents_task():
    """
    Pre-processing step: extract text from files in all storage folders,
    find matching reference numbers from the orders table, and rename files
    by prepending the reference so the scan step can match them.
    """
    import asyncio
    from app.services.document_prereader_service import preread_all_folders

    async def run():
        engine, SessionLocal = _make_session_factory()
        try:
            async with SessionLocal() as db:
                result = await preread_all_folders(db)
                return result
        finally:
            await engine.dispose()

    result = asyncio.run(run())
    logger.info("preread_documents_task complete: %s", result)
    return result


@celery_app.task(name="app.core.tasks.check_ollama_task")
def check_ollama_task():
    """
    Periodic task: check if Ollama is reachable.
    If not, send an alert email to all active admin users.
    """
    import asyncio
    import httpx
    from sqlalchemy import select
    from app.models.models import User
    from app.core.config import settings

    async def run():
        engine, SessionLocal = _make_session_factory()
        try:
            async with SessionLocal() as db:
                # Read Ollama URL from SystemConfig
                from app.models.models import SystemConfig
                row = await db.get(SystemConfig, "ollama_base_url")
                ollama_url = (row.value.strip() if row and row.value else None) or settings.OLLAMA_BASE_URL

                # Check Ollama health
                ollama_up = False
                try:
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        resp = await client.get(ollama_url)
                        ollama_up = resp.status_code < 500
                except Exception as exc:
                    logger.warning("check_ollama_task: Ollama unreachable — %s", exc)

                if ollama_up:
                    logger.info("check_ollama_task: Ollama is up at %s", ollama_url)
                    return

                # Ollama is down — fetch all active admins and super_admins
                result = await db.execute(
                    select(User).where(User.role.in_(["admin", "super_admin"]), User.is_active == True)
                )
                admins = result.scalars().all()
                if not admins:
                    logger.warning("check_ollama_task: Ollama is down but no active admin users found")
                    return

                from app.services.email_service import send_email
                subject = "⚠ Ollama Service Unreachable"
                body = (
                    f"This is an automated alert from the POD System.\n\n"
                    f"Ollama is not reachable at {ollama_url}.\n\n"
                    f"Email classification and pipeline processing will fail until Ollama is restored.\n\n"
                    f"Please start the Ollama service on the host machine and verify it is accessible."
                )
                for admin in admins:
                    await send_email(db, to=admin.email, subject=subject, body=body)
                    logger.info("check_ollama_task: alert sent to %s", admin.email)

        finally:
            await engine.dispose()

    asyncio.run(run())


@celery_app.task(name="app.core.tasks.hard_delete_retention_task")
def hard_delete_retention_task():
    """
    Periodic task: permanently delete soft-deleted records older than 90 days.
    Applies to email_requests, approval_queue, and guidance_queue rows where
    line_deletion_flag=TRUE and received_at/created_at < NOW() - 90 days.
    """
    import asyncio
    from sqlalchemy import text

    async def run():
        engine, SessionLocal = _make_session_factory()
        try:
            async with SessionLocal() as db:
                result = await db.execute(text("""
                    DELETE FROM email_requests
                    WHERE line_deletion_flag = TRUE
                      AND received_at < datetime('now', '-90 days')
                """))
                deleted = result.rowcount or 0
                await db.commit()
                logger.info("hard_delete_retention_task: hard-deleted %d email_request(s)", deleted)
        finally:
            await engine.dispose()

    asyncio.run(run())


@celery_app.task(name="app.core.tasks.heartbeat_task")
def heartbeat_task():
    """
    Periodic heartbeat: collect system diagnostics and email a status
    report to the vendor.  Controlled by system_config 'heartbeat_enabled'.
    """
    import asyncio
    import httpx
    import shutil
    import platform
    from datetime import datetime, timedelta
    from sqlalchemy import select, func
    from app.models.models import User, MonitoredEmail, Order, EmailRequest, SystemConfig
    from app.core.config import settings
    from app.services.email_service import send_email

    async def run():
        engine, SessionLocal = _make_session_factory()
        try:
            async with SessionLocal() as db:
                # Check if heartbeat is enabled
                row = await db.execute(
                    select(SystemConfig).where(SystemConfig.key == "heartbeat_enabled")
                )
                cfg_row = row.scalar_one_or_none()
                if not cfg_row or cfg_row.value.lower() not in ("true", "1", "yes"):
                    logger.debug("heartbeat_task: disabled, skipping")
                    return

                # Get vendor email
                row = await db.execute(
                    select(SystemConfig).where(SystemConfig.key == "heartbeat_recipient")
                )
                recip_row = row.scalar_one_or_none()
                vendor_email = (recip_row.value if recip_row else None) or settings.VENDOR_EMAIL
                if not vendor_email:
                    logger.warning("heartbeat_task: no recipient configured")
                    return

                now = datetime.now()
                hostname = platform.node()

                # ── Collect stats ─────────────────────────────
                # Services
                url_row = await db.get(SystemConfig, "ollama_base_url")
                ollama_url = (url_row.value.strip() if url_row and url_row.value else None) or settings.OLLAMA_BASE_URL
                ollama_up = False
                try:
                    async with httpx.AsyncClient(timeout=5.0) as c:
                        resp = await c.get(ollama_url)
                        ollama_up = resp.status_code < 500
                except Exception:
                    pass

                redis_up = False
                try:
                    import redis
                    r = redis.Redis.from_url(settings.REDIS_URL, socket_timeout=3)
                    r.ping()
                    redis_up = True
                except Exception:
                    pass

                # DB counts
                total_orders = (await db.execute(select(func.count()).select_from(Order))).scalar()
                total_requests = (await db.execute(select(func.count()).select_from(EmailRequest))).scalar()

                cutoff_24h = now - timedelta(hours=24)
                failed_24h = (await db.execute(
                    select(func.count()).select_from(EmailRequest).where(
                        EmailRequest.status == "failed",
                        EmailRequest.received_at >= cutoff_24h,
                    )
                )).scalar()

                active_emails = (await db.execute(
                    select(func.count()).select_from(MonitoredEmail).where(MonitoredEmail.status == "active")
                )).scalar()

                active_users = (await db.execute(
                    select(func.count()).select_from(User).where(User.is_active == True)
                )).scalar()

                # Disk
                try:
                    usage = shutil.disk_usage(os.path.abspath("."))
                    disk_free_gb = round(usage.free / (1024**3), 1)
                    disk_pct = round(usage.used / usage.total * 100, 1)
                except Exception:
                    disk_free_gb = "?"
                    disk_pct = "?"

                # Version
                version = "unknown"
                for p in ["../VERSION", "VERSION", "../../VERSION"]:
                    ap = os.path.abspath(p)
                    if os.path.isfile(ap):
                        with open(ap) as f:
                            version = f.read().strip()
                        break

                # ── Build email ───────────────────────────────
                ok = lambda v: "OK" if v else "DOWN"
                subject = f"Heartbeat: {hostname} - v{version} - {'OK' if (ollama_up and redis_up and failed_24h == 0) else 'ISSUES'}"

                body = f"""Document Retrieval System - Heartbeat Report
{'=' * 50}

Host:       {hostname}
Version:    v{version}
Timestamp:  {now.strftime('%Y-%m-%d %H:%M')}
OS:         {platform.system()} {platform.release()}

SERVICES
  Ollama:   {ok(ollama_up)}
  Redis:    {ok(redis_up)}

DATABASE
  Orders:           {total_orders}
  Email requests:   {total_requests}
  Failed (24h):     {failed_24h}
  Monitored emails: {active_emails} active
  Users:            {active_users} active

DISK
  Free:    {disk_free_gb} GB
  Used:    {disk_pct}%

{'=' * 50}
This is an automated heartbeat from the DOC Retrieval System.
To disable, set heartbeat_enabled = false in System Config.
"""

                result = await send_email(db, to=vendor_email, subject=subject, body=body)
                if result.get("sent"):
                    logger.info("heartbeat_task: report sent to %s", vendor_email)
                else:
                    logger.warning("heartbeat_task: failed to send — %s", result.get("error"))

        finally:
            await engine.dispose()

    asyncio.run(run())


@celery_app.task(name="app.core.tasks.oauth_reauth_reminder_task")
def oauth_reauth_reminder_task():
    """
    Periodic task: find monitored mailboxes whose Microsoft OAuth2 grant has
    expired (or is about to expire) and email them a link to re-authorize.
    """
    import asyncio
    import secrets
    from datetime import datetime, timedelta
    from sqlalchemy import select, or_
    from app.models.models import MonitoredEmail, SystemConfig
    from app.services.email_service import send_email

    REAUTH_WARNING_DAYS      = 7
    REMINDER_INTERVAL_HOURS  = 24
    REAUTH_TOKEN_HOURS       = 72

    async def run():
        engine, SessionLocal = _make_session_factory()
        try:
            async with SessionLocal() as db:
                now = datetime.now()
                warn_before = now + timedelta(days=REAUTH_WARNING_DAYS)

                row = await db.get(SystemConfig, "app_base_url")
                base_url = (row.value or "").strip().rstrip("/") if row else ""

                result = await db.execute(
                    select(MonitoredEmail).where(
                        MonitoredEmail.auth_type == "oauth_microsoft",
                        or_(
                            MonitoredEmail.status == "reauth_required",
                            MonitoredEmail.oauth_token_expires_at == None,   # noqa: E711
                            MonitoredEmail.oauth_token_expires_at <= warn_before,
                        ),
                    )
                )
                candidates = result.scalars().all()
                if not candidates:
                    logger.debug("oauth_reauth_reminder_task: nothing due")
                    return

                for me in candidates:
                    if me.last_reauth_reminder_at and (
                        now - me.last_reauth_reminder_at
                    ) < timedelta(hours=REMINDER_INTERVAL_HOURS):
                        continue

                    me.setup_token      = secrets.token_urlsafe(32)
                    me.token_expires_at = now + timedelta(hours=REAUTH_TOKEN_HOURS)
                    setup_url = (
                        f"{base_url}/setup-email?token={me.setup_token}"
                        if base_url else f"/setup-email?token={me.setup_token}"
                    )

                    expires_txt = (
                        me.oauth_token_expires_at.strftime("%Y-%m-%d %H:%M UTC")
                        if me.oauth_token_expires_at else "already expired"
                    )
                    is_expired = me.status == "reauth_required" or (
                        me.oauth_token_expires_at and me.oauth_token_expires_at <= now
                    )

                    subject = (
                        "Action required: re-authorize your Microsoft 365 mailbox"
                        if is_expired else
                        "Your Microsoft 365 mailbox authorization expires soon"
                    )
                    lead = (
                        "Your Microsoft 365 authorization for the POD Automation System "
                        "has expired (or been revoked)."
                        if is_expired else
                        f"Your Microsoft 365 authorization for the POD Automation System "
                        f"will expire on {expires_txt}."
                    )

                    body_html = f"""
<html><body style="font-family:Arial,sans-serif;max-width:620px;margin:40px auto;color:#333;font-size:15px;line-height:1.6">
  <h2 style="color:#0d6efd">POD Automation System</h2>
  <p>Hi,</p>
  <p>{lead}</p>
  <p>Until you re-authorize, new emails to <strong>{me.email}</strong> will no longer be
  picked up automatically. Please click the button below to sign in with Microsoft again.</p>
  <p style="text-align:center;margin:32px 0">
    <a href="{setup_url}"
       style="background:#0d6efd;color:#fff;padding:14px 32px;border-radius:6px;
              text-decoration:none;font-weight:bold;font-size:16px;display:inline-block">
      Re-authorize Mailbox
    </a>
  </p>
  <p style="font-size:13px;color:#666">
    This link is valid for {REAUTH_TOKEN_HOURS} hours. If it expires, ask your administrator
    to send a new invitation.
  </p>
  <p style="font-size:13px;color:#666">
    Can't click the button? Copy this link into your browser:<br>
    <code style="background:#f5f5f5;padding:3px 6px;border-radius:4px;font-size:12px;word-break:break-all">{setup_url}</code>
  </p>
  <hr style="border:none;border-top:1px solid #dee2e6;margin:24px 0"/>
  <p style="font-size:12px;color:#aaa">Sent automatically by the POD Automation System.</p>
</body></html>
""".strip()

                    result = await send_email(db, to=me.email, subject=subject, body=body_html)
                    if result.get("sent"):
                        me.last_reauth_reminder_at = now
                        logger.info("oauth_reauth_reminder_task: reminder sent to %s (expires %s)",
                                    me.email, expires_txt)
                    else:
                        logger.warning("oauth_reauth_reminder_task: failed to send to %s — %s",
                                       me.email, result.get("error"))
                    await db.commit()
        finally:
            await engine.dispose()

    asyncio.run(run())


@celery_app.task(name="app.core.tasks.poll_ftp_task")
def poll_ftp_task():
    """Periodic task: poll FTP server for new POD files."""
    import asyncio
    from app.services.ftp_service import poll_ftp
    from app.core.config import settings

    async def run():
        engine, SessionLocal = _make_session_factory()
        try:
            async with SessionLocal() as db:
                from app.models.models import SystemConfig
                url_row = await db.get(SystemConfig, "ollama_base_url")
                model_row = await db.get(SystemConfig, "ollama_model")
                ollama_url = (url_row.value.strip() if url_row and url_row.value else None) or settings.OLLAMA_BASE_URL
                ollama_model = (model_row.value.strip() if model_row and model_row.value else None) or settings.OLLAMA_MODEL
                result = await poll_ftp(db, ollama_url, ollama_model)
                logger.info(f"FTP poll result: {result}")
                return result
        finally:
            await engine.dispose()

    return asyncio.run(run())
