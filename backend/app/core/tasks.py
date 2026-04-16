from app.core.celery_app import celery_app
import logging

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
                # Check Ollama health
                ollama_up = False
                try:
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        resp = await client.get(settings.OLLAMA_BASE_URL)
                        ollama_up = resp.status_code < 500
                except Exception as exc:
                    logger.warning("check_ollama_task: Ollama unreachable — %s", exc)

                if ollama_up:
                    logger.info("check_ollama_task: Ollama is up at %s", settings.OLLAMA_BASE_URL)
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
                    f"Ollama is not reachable at {settings.OLLAMA_BASE_URL}.\n\n"
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
                result = await poll_ftp(db, settings.OLLAMA_BASE_URL, settings.OLLAMA_MODEL)
                logger.info(f"FTP poll result: {result}")
                return result
        finally:
            await engine.dispose()

    return asyncio.run(run())
