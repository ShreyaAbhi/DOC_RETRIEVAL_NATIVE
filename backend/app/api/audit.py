from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, outerjoin, or_, String
from typing import Optional

from app.db.session import get_db
from app.models.models import AuditLog, EmailRequest, User
from app.core.security import get_current_user

router = APIRouter()


@router.get("")
async def list_audit_logs(
    request_id: Optional[str] = None,
    action: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    q = select(AuditLog).order_by(desc(AuditLog.created_at))
    if request_id:
        q = q.where(AuditLog.request_id == request_id)
    if action:
        q = q.where(AuditLog.action == action)
    if search:
        term = f"%{search}%"
        q = q.where(or_(
            AuditLog.summary.ilike(term),
            AuditLog.action.ilike(term),
            AuditLog.request_id.cast(String).ilike(term),
        ))
        limit = 2000
    q = q.limit(limit).offset(offset)
    result = await db.execute(q)
    logs = result.scalars().all()

    # Fetch reference numbers for all unique request_ids in one query
    req_ids = list({l.request_id for l in logs if l.request_id})
    ref_map = {}
    if req_ids:
        refs = await db.execute(
            select(EmailRequest.id, EmailRequest.reference_number)
            .where(EmailRequest.id.in_(req_ids))
        )
        ref_map = {str(row.id): row.reference_number for row in refs.all()}

    return [_out(a, ref_map.get(str(a.request_id))) for a in logs]


@router.get("/trace/{request_id}")
async def trace_request(request_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    req = await db.get(EmailRequest, request_id)
    if not req:
        raise HTTPException(404, "Request not found")
    result = await db.execute(
        select(AuditLog)
        .where(AuditLog.request_id == request_id)
        .order_by(AuditLog.created_at)
    )
    logs = result.scalars().all()
    return {
        "request": {
            "id": str(req.id),
            "reference": req.reference_number,
            "from": req.from_email,
            "subject": req.subject,
            "status": req.status,
            "received_at": req.received_at.isoformat() if req.received_at else None,
        },
        "timeline": [_out(a, req.reference_number) for a in logs],
    }


def _out(a: AuditLog, reference_number: str = None):
    return {
        "id": a.id,
        "request_id": str(a.request_id) if a.request_id else None,
        "reference_number": reference_number,
        "action": a.action,
        "actor": a.actor,
        "summary": a.summary,
        "detail": a.detail,
        "duration_ms": a.duration_ms,
        "success": a.success,
        "error_detail": a.error_detail,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }
