"""
Remote diagnostics endpoint — gives the vendor a full picture of system
health without needing physical access to the customer machine.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, text, func
from datetime import datetime, timedelta
import platform
import shutil
import os
import logging

from app.db.session import get_db
from app.models.models import (
    User, MonitoredEmail, Order, EmailRequest, SystemConfig,
)
from app.core.config import settings
from app.core.security import require_admin

router = APIRouter()
logger = logging.getLogger(__name__)


async def _check_service(url: str, timeout: float = 5.0) -> dict:
    """Ping a local service and return status + latency."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            t0 = datetime.utcnow()
            resp = await c.get(url)
            latency_ms = (datetime.utcnow() - t0).total_seconds() * 1000
            return {"up": resp.status_code < 500, "status_code": resp.status_code,
                    "latency_ms": round(latency_ms, 1)}
    except Exception as e:
        return {"up": False, "error": str(e)}


async def _check_redis() -> dict:
    """Check Redis connectivity."""
    import redis
    try:
        r = redis.Redis.from_url(settings.REDIS_URL, socket_timeout=3)
        r.ping()
        info = r.info(section="memory")
        return {"up": True, "used_memory_mb": round(info.get("used_memory", 0) / 1048576, 1)}
    except Exception as e:
        return {"up": False, "error": str(e)}


@router.get("/status")
async def full_diagnostics(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Comprehensive system health report."""

    now = datetime.utcnow()
    report = {
        "generated_at": now.isoformat() + "Z",
        "version": _read_version(),
    }

    # ── Host info ─────────────────────────────────────────────
    report["host"] = {
        "hostname": platform.node(),
        "os": f"{platform.system()} {platform.release()}",
        "os_version": platform.version(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
    }

    # ── Disk space ────────────────────────────────────────────
    try:
        usage = shutil.disk_usage(os.path.abspath("."))
        report["disk"] = {
            "total_gb": round(usage.total / (1024**3), 1),
            "used_gb": round(usage.used / (1024**3), 1),
            "free_gb": round(usage.free / (1024**3), 1),
            "percent_used": round(usage.used / usage.total * 100, 1),
        }
    except Exception as e:
        report["disk"] = {"error": str(e)}

    # ── Storage folders ───────────────────────────────────────
    storage_dirs = {
        "pod_storage": settings.POD_STORAGE_PATH,
        "packing_slips": settings.PACKING_SLIPS_PATH,
        "invoices": settings.INVOICES_PATH,
        "documents": settings.DOCUMENTS_PATH,
        "order_import": settings.ORDER_IMPORT_PATH,
    }
    folder_stats = {}
    for name, path in storage_dirs.items():
        abs_path = os.path.abspath(path)
        if os.path.isdir(abs_path):
            files = [f for f in os.listdir(abs_path) if os.path.isfile(os.path.join(abs_path, f))]
            total_size = sum(os.path.getsize(os.path.join(abs_path, f)) for f in files)
            folder_stats[name] = {"exists": True, "file_count": len(files),
                                  "total_size_mb": round(total_size / 1048576, 1)}
        else:
            folder_stats[name] = {"exists": False}
    report["storage"] = folder_stats

    # ── Services ──────────────────────────────────────────────
    import asyncio
    url_row = await db.get(SystemConfig, "ollama_base_url")
    ollama_url = (url_row.value.strip() if url_row and url_row.value else None) or settings.OLLAMA_BASE_URL
    ollama_check = _check_service(ollama_url)
    backend_check = _check_service(f"http://localhost:{settings.BACKEND_PORT if hasattr(settings, 'BACKEND_PORT') else 8002}/health")
    redis_check = _check_redis()

    ollama_result, backend_result, redis_result = await asyncio.gather(
        ollama_check, backend_check, redis_check, return_exceptions=True
    )

    report["services"] = {
        "ollama": ollama_result if isinstance(ollama_result, dict) else {"error": str(ollama_result)},
        "backend": backend_result if isinstance(backend_result, dict) else {"error": str(backend_result)},
        "redis": redis_result if isinstance(redis_result, dict) else {"error": str(redis_result)},
    }

    # ── Database stats ────────────────────────────────────────
    try:
        db_stats = {}

        result = await db.execute(select(func.count()).select_from(User))
        db_stats["total_users"] = result.scalar()

        result = await db.execute(select(func.count()).select_from(User).where(User.is_active == True))
        db_stats["active_users"] = result.scalar()

        result = await db.execute(select(func.count()).select_from(Order))
        db_stats["total_orders"] = result.scalar()

        result = await db.execute(select(func.count()).select_from(EmailRequest))
        db_stats["total_email_requests"] = result.scalar()

        result = await db.execute(
            select(func.count()).select_from(EmailRequest).where(EmailRequest.status == "failed")
        )
        db_stats["failed_requests"] = result.scalar()

        # Recent failures (last 24h)
        cutoff = now - timedelta(hours=24)
        result = await db.execute(
            select(func.count()).select_from(EmailRequest).where(
                EmailRequest.status == "failed",
                EmailRequest.received_at >= cutoff,
            )
        )
        db_stats["failed_last_24h"] = result.scalar()

        result = await db.execute(select(func.count()).select_from(MonitoredEmail))
        db_stats["monitored_emails"] = result.scalar()

        result = await db.execute(
            select(func.count()).select_from(MonitoredEmail).where(MonitoredEmail.status == "active")
        )
        db_stats["active_monitored_emails"] = result.scalar()

        # DB file size
        db_path = os.path.abspath("pod_system.db")
        if os.path.exists(db_path):
            db_stats["db_size_mb"] = round(os.path.getsize(db_path) / 1048576, 1)

        report["database"] = db_stats
    except Exception as e:
        report["database"] = {"error": str(e)}

    # ── SMTP config ───────────────────────────────────────────
    try:
        result = await db.execute(
            select(SystemConfig).where(SystemConfig.key.in_([
                'smtp_host', 'smtp_port', 'smtp_user', 'smtp_from',
                'app_base_url', 'heartbeat_enabled',
            ]))
        )
        cfg = {r.key: r.value for r in result.scalars().all()}
        report["config"] = {
            "smtp_host": cfg.get("smtp_host", ""),
            "smtp_port": cfg.get("smtp_port", ""),
            "smtp_user": cfg.get("smtp_user", ""),
            "smtp_from": cfg.get("smtp_from", ""),
            "app_base_url": cfg.get("app_base_url", ""),
            "heartbeat_enabled": cfg.get("heartbeat_enabled", "false"),
            "smtp_password_set": bool(cfg.get("smtp_password", "")),
        }
    except Exception as e:
        report["config"] = {"error": str(e)}

    # ── Recent errors (last 10 failed requests) ──────────────
    try:
        result = await db.execute(
            select(EmailRequest)
            .where(EmailRequest.status == "failed")
            .order_by(EmailRequest.received_at.desc())
            .limit(10)
        )
        failures = result.scalars().all()
        report["recent_errors"] = [
            {
                "id": str(f.id),
                "subject": f.subject[:80] if f.subject else None,
                "error": f.error_message[:200] if f.error_message else None,
                "received_at": f.received_at.isoformat() if f.received_at else None,
            }
            for f in failures
        ]
    except Exception as e:
        report["recent_errors"] = {"error": str(e)}

    return report


def _read_version() -> str:
    """Read VERSION file from project root."""
    for path in ["../VERSION", "VERSION", "../../VERSION"]:
        abs_path = os.path.abspath(path)
        if os.path.isfile(abs_path):
            with open(abs_path) as f:
                return f.read().strip()
    return "unknown"
