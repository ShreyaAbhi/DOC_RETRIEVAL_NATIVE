from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc, text, String, or_
from typing import Optional
from datetime import datetime, timedelta

from app.db.session import get_db
from app.models.models import EmailRequest, AuditLog, ApprovalQueue, GuidanceQueue, User
from app.core.security import get_current_user

router = APIRouter()


@router.get("/summary")
async def summary_report(
    days: int = 30,
    exclude_deleted: bool = True,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    since = datetime.now() - timedelta(days=days)

    # Base filter applied to all email_requests queries
    def base_filter(stmt):
        stmt = stmt.where(EmailRequest.received_at >= since)
        if exclude_deleted:
            stmt = stmt.where(
                (EmailRequest.line_deletion_flag == False) |
                EmailRequest.line_deletion_flag.is_(None)
            )
        return stmt

    status_result = await db.execute(
        base_filter(
            select(EmailRequest.status, func.count().label("count"))
        ).group_by(EmailRequest.status)
    )
    by_status = {row.status: row.count for row in status_result}
    total     = sum(by_status.values())
    completed = by_status.get("completed", 0)
    failed    = by_status.get("failed", 0)

    # Count pending from the actual queue tables, not email_requests.status.
    # email_requests rows can be stuck at awaiting_guidance/awaiting_approval
    # long after their queue entry was resolved — queue tables are authoritative.
    guid_pending_q = (
        select(func.count())
        .select_from(GuidanceQueue)
        .join(EmailRequest, EmailRequest.id == GuidanceQueue.request_id)
        .where(GuidanceQueue.status.cast(String) == "pending")
        .where(
            (GuidanceQueue.line_deletion_flag == False) |
            GuidanceQueue.line_deletion_flag.is_(None)
        )
    )
    appr_pending_q = (
        select(func.count())
        .select_from(ApprovalQueue)
        .join(EmailRequest, EmailRequest.id == ApprovalQueue.request_id)
        .where(ApprovalQueue.status.cast(String) == "pending")
    )
    if exclude_deleted:
        deletion_filter = (
            (EmailRequest.line_deletion_flag == False) |
            EmailRequest.line_deletion_flag.is_(None)
        )
        guid_pending_q = guid_pending_q.where(deletion_filter)
        appr_pending_q = appr_pending_q.where(deletion_filter)

    guid_pending = await db.scalar(guid_pending_q) or 0
    appr_pending = await db.scalar(appr_pending_q) or 0
    pending = guid_pending + appr_pending

    conf_result = await db.execute(
        base_filter(
            select(func.avg(EmailRequest.confidence_score))
        ).where(EmailRequest.confidence_score.isnot(None))
    )
    avg_conf = conf_result.scalar()

    appr_result = await db.execute(
        select(ApprovalQueue.status, func.count().label("c"))
        .where(
            (ApprovalQueue.line_deletion_flag == False) |
            ApprovalQueue.line_deletion_flag.is_(None)
        )
        .group_by(ApprovalQueue.status)
    )
    approvals = {row.status: row.c for row in appr_result}

    guid_result = await db.execute(
        select(GuidanceQueue.status, func.count().label("c"))
        .join(EmailRequest, EmailRequest.id == GuidanceQueue.request_id)
        .where(
            (GuidanceQueue.line_deletion_flag == False) |
            GuidanceQueue.line_deletion_flag.is_(None)
        )
        .where(
            (EmailRequest.line_deletion_flag == False) |
            EmailRequest.line_deletion_flag.is_(None)
        )
        .group_by(GuidanceQueue.status)
    )
    guidance = {row.status: row.c for row in guid_result}

    if exclude_deleted:
        daily_result = await db.execute(text("""
            SELECT date(received_at) as day, COUNT(*) as count
            FROM email_requests
            WHERE received_at >= datetime('now', '-14 days')
              AND (line_deletion_flag = FALSE OR line_deletion_flag IS NULL)
            GROUP BY date(received_at)
            ORDER BY day
        """))
    else:
        daily_result = await db.execute(text("""
            SELECT date(received_at) as day, COUNT(*) as count
            FROM email_requests
            WHERE received_at >= datetime('now', '-14 days')
            GROUP BY date(received_at)
            ORDER BY day
        """))
    daily = [{"date": str(row.day), "count": row.count} for row in daily_result]

    intent_result = await db.execute(
        base_filter(
            select(EmailRequest.intent, func.count().label("c"))
        ).group_by(EmailRequest.intent)
    )
    by_intent = {row.intent: row.c for row in intent_result if row.intent}

    return {
        "period_days": days,
        "total_requests": total,
        "completed": completed,
        "failed": failed,
        "pending": pending,
        "success_rate": round(completed / total * 100, 1) if total else 0,
        "avg_confidence": round(float(avg_conf), 1) if avg_conf else 0,
        "by_status": by_status,
        "by_intent": by_intent,
        "approvals": approvals,
        "guidance": guidance,
        "daily_volume": daily,
    }


@router.get("/requests")
async def requests_report(
    status: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 200,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    if search and len(search) > 200:
        raise HTTPException(400, "Search term too long — maximum 200 characters")
    q = select(EmailRequest).order_by(desc(EmailRequest.received_at))
    if status:
        q = q.where(EmailRequest.status.cast(String) == status)
    if from_date:
        q = q.where(EmailRequest.received_at >= datetime.fromisoformat(from_date))
    if to_date:
        q = q.where(EmailRequest.received_at <= datetime.fromisoformat(to_date))
    if search:
        term = f"%{search}%"
        q = q.where(or_(
            EmailRequest.reference_number.ilike(term),
            EmailRequest.from_email.ilike(term),
            EmailRequest.from_name.ilike(term),
            EmailRequest.subject.ilike(term),
            EmailRequest.extracted_order_id.ilike(term),
            EmailRequest.status.cast(String).ilike(term),
            EmailRequest.intent.ilike(term),
        ))
        limit = 1000
    result = await db.execute(q.limit(limit))
    return [{
        "id": str(r.id),
        "reference_number": r.reference_number,
        "from_email": r.from_email,
        "subject": r.subject,
        "status": r.status,
        "intent": r.intent,
        "confidence_score": float(r.confidence_score) if r.confidence_score else None,
        "extracted_order_id": r.extracted_order_id,
        "requires_guidance": r.requires_guidance,
        "received_at": r.received_at.isoformat() if r.received_at else None,
        "completed_at": r.completed_at.isoformat() if r.completed_at else None,
    } for r in result.scalars().all()]


@router.get("/audit-activity")
async def audit_activity(limit: int = 200, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    result = await db.execute(
        select(AuditLog).order_by(desc(AuditLog.created_at)).limit(limit)
    )
    return [{
        "id": a.id,
        "request_id": str(a.request_id) if a.request_id else None,
        "action": a.action,
        "actor": a.actor,
        "summary": a.summary,
        "success": a.success,
        "duration_ms": a.duration_ms,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    } for a in result.scalars().all()]
