from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, String, delete, or_
from pydantic import BaseModel
from typing import Optional, List

from app.db.session import get_db
from app.models.models import EmailRequest, User, GuidanceQueue, ApprovalQueue
from app.agents.pipeline import make_ref
from app.services.audit_service import log_audit
from app.core.security import get_current_user
from app.core.tasks import process_email_task

router = APIRouter()


class InboundEmail(BaseModel):
    from_email: str
    from_name: Optional[str] = None
    subject: str
    body: str


class RetriggerRequest(BaseModel):
    ids: List[str]


class SoftDeleteRequest(BaseModel):
    ids: List[str]


@router.post("")
async def submit_email(email: InboundEmail, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    req = EmailRequest(
        reference_number=make_ref(),
        from_email=email.from_email,
        from_name=email.from_name,
        subject=email.subject,
        body=email.body,
    )
    db.add(req)
    await db.flush()
    await log_audit(db, req.id, "email_received",
                    f"Email received from {email.from_email}",
                    {"subject": email.subject})
    await db.commit()
    process_email_task.delay(str(req.id))
    return {"id": str(req.id), "reference": req.reference_number, "status": "received"}


@router.post("/retrigger")
async def retrigger_requests(
    body: RetriggerRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Reset and re-run the pipeline for the given request IDs."""
    if not body.ids:
        raise HTTPException(400, "No IDs provided")

    triggered = 0
    for req_id in body.ids:
        req = await db.get(EmailRequest, req_id)
        if not req:
            continue
        await db.execute(delete(GuidanceQueue).where(GuidanceQueue.request_id == req.id))
        await db.execute(delete(ApprovalQueue).where(ApprovalQueue.request_id == req.id))
        req.status = "received"
        req.is_pod_request = None
        req.confidence_score = None
        req.intent = None
        req.requires_guidance = False
        req.error_message = None
        req.completed_at = None
        req.response_subject = None
        req.response_body = None
        req.response_sent_at = None
        await log_audit(db, req.id, "system", "Pipeline retriggered by user", actor="admin")
        triggered += 1

    await db.commit()

    # Enqueue after commit so the worker sees the reset state
    for req_id in body.ids:
        process_email_task.delay(req_id)

    return {"triggered": triggered}


@router.delete("/soft-delete")
async def soft_delete_requests(
    body: SoftDeleteRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Soft-delete requests by setting line_deletion_flag = True. Rows are kept in the DB."""
    if not body.ids:
        raise HTTPException(400, "No IDs provided")
    flagged = 0
    for req_id in body.ids:
        req = await db.get(EmailRequest, req_id)
        if not req:
            continue
        req.line_deletion_flag = True
        await log_audit(db, req.id, "system",
                        f"Request soft-deleted by {current_user.email}",
                        {"reference_number": req.reference_number},
                        actor=current_user.email)
        flagged += 1
    await db.commit()
    return {"flagged": flagged}


@router.get("")
async def list_requests(
    status: Optional[str] = None,
    search: Optional[str] = None,
    include_deleted: bool = False,
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    stmt = select(EmailRequest).order_by(desc(EmailRequest.received_at))
    if not include_deleted:
        stmt = stmt.where(
            (EmailRequest.line_deletion_flag == False) |
            EmailRequest.line_deletion_flag.is_(None)
        )
    if status:
        stmt = stmt.where(EmailRequest.status.cast(String) == status)
    if search:
        term = f"%{search}%"
        stmt = stmt.where(or_(
            EmailRequest.reference_number.ilike(term),
            EmailRequest.from_email.ilike(term),
            EmailRequest.from_name.ilike(term),
            EmailRequest.subject.ilike(term),
            EmailRequest.status.cast(String).ilike(term),
            EmailRequest.extracted_order_id.ilike(term),
        ))
        limit = 500
    stmt = stmt.limit(limit).offset(offset)
    result = await db.execute(stmt)
    return [_out(r) for r in result.scalars().all()]


@router.get("/{request_id}")
async def get_request(request_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    r = await db.get(EmailRequest, request_id)
    if not r:
        raise HTTPException(404)
    return _out(r)


def _out(r: EmailRequest):
    return {
        "id": str(r.id),
        "reference_number": r.reference_number,
        "from_email": r.from_email,
        "from_name": r.from_name,
        "subject": r.subject,
        "body": r.body,
        "received_at": r.received_at.isoformat() if r.received_at else None,
        "status": r.status,
        "is_pod_request": r.is_pod_request,
        "confidence_score": float(r.confidence_score) if r.confidence_score else None,
        "intent": r.intent,
        "extracted_order_id": r.extracted_order_id,
        "extracted_tracking": r.extracted_tracking,
        "pod_document_id": str(r.pod_document_id) if r.pod_document_id else None,
        "packing_slip_document_id": str(r.packing_slip_document_id) if r.packing_slip_document_id else None,
        "invoice_document_id": str(r.invoice_document_id) if r.invoice_document_id else None,
        "response_subject": r.response_subject,
        "response_body": r.response_body,
        "response_sent_at": r.response_sent_at.isoformat() if r.response_sent_at else None,
        "requires_guidance": r.requires_guidance,
        "error_message": r.error_message,
        "completed_at": r.completed_at.isoformat() if r.completed_at else None,
        "line_deletion_flag": bool(r.line_deletion_flag),
    }
