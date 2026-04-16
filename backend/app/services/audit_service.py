from sqlalchemy.ext.asyncio import AsyncSession
from app.models.models import AuditLog
from typing import Optional
import uuid


async def log_audit(
    db: AsyncSession,
    request_id,
    action: str,
    summary: str,
    detail: dict = None,
    actor: str = "system",
    duration_ms: int = None,
    success: bool = True,
    error_detail: str = None,
):
    log = AuditLog(
        request_id=request_id,
        action=action,
        actor=actor,
        summary=summary,
        detail=detail or {},
        duration_ms=duration_ms,
        success=success,
        error_detail=error_detail,
    )
    db.add(log)
    await db.flush()
    return log
