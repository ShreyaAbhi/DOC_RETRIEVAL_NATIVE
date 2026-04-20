from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, String
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
import uuid

from app.db.session import get_db
from app.models.models import GuidanceQueue, EmailRequest, User
from app.services.audit_service import log_audit
from app.core.security import get_current_user, require_reviewer_or_admin

router = APIRouter()


class GuidanceResponse(BaseModel):
    guidance: str
    provided_by: Optional[str] = "admin"
    proceed: bool = True
    customer_po: Optional[str] = None
    delivery_number: Optional[str] = None
    order_reference: Optional[str] = None


class RetriggerWithReferenceRequest(BaseModel):
    customer_po: Optional[str] = None
    delivery_number: Optional[str] = None


class BulkDeleteRequest(BaseModel):
    ids: List[str]


@router.get("")
async def list_guidance(status: Optional[str] = "pending", db: AsyncSession = Depends(get_db), _: User = Depends(require_reviewer_or_admin)):
    q = (
        select(GuidanceQueue)
        .where(
            (GuidanceQueue.line_deletion_flag == False) |
            GuidanceQueue.line_deletion_flag.is_(None)
        )
        .order_by(desc(GuidanceQueue.created_at))
    )
    if status:
        q = q.where(GuidanceQueue.status.cast(String) == status)
    result = await db.execute(q)
    out = []
    for g in result.scalars().all():
        req = await db.get(EmailRequest, g.request_id)
        out.append({
            "id": str(g.id),
            "request_id": str(g.request_id),
            "status": g.status,
            "reason": g.reason,
            "confidence": float(g.confidence) if g.confidence else None,
            "agent_question": g.agent_question,
            "human_guidance": g.human_guidance,
            "created_at": g.created_at.isoformat() if g.created_at else None,
            "request": {
                "from_email": req.from_email,
                "subject": req.subject,
                "body": req.body,
                "reference_number": req.reference_number,
                "intent": req.intent,
            } if req else None,
        })
    return out


@router.post("/{guidance_id}/respond")
async def provide_guidance(
    guidance_id: str, body: GuidanceResponse, db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_reviewer_or_admin)
):
    from app.agents.pipeline import resume_after_guidance

    g = await db.get(GuidanceQueue, guidance_id)
    if not g:
        raise HTTPException(404)

    g.status = "provided"
    g.human_guidance = body.guidance
    g.provided_by = body.provided_by
    g.provided_at = datetime.now()

    req = await db.get(EmailRequest, g.request_id)
    if req:
        # Apply any identifiers provided by the human reviewer
        if body.delivery_number:
            req.extracted_tracking = body.delivery_number.strip()
        if body.customer_po:
            req.extracted_order_id = body.customer_po.strip()
        elif body.order_reference:
            req.extracted_order_id = body.order_reference.strip()

        await log_audit(db, req.id, "guidance_provided",
                        f"Guidance provided by {body.provided_by}",
                        {"guidance": body.guidance, "proceed": body.proceed,
                         "customer_po": body.customer_po, "delivery_number": body.delivery_number,
                         "order_reference": body.order_reference},
                        actor=body.provided_by)
        if body.proceed:
            await db.commit()
            await resume_after_guidance(db, str(req.id))
        else:
            req.status = "completed"
            req.completed_at = datetime.now()

    await db.commit()
    return {"status": "guidance_provided"}


@router.post("/{guidance_id}/retrigger")
async def retrigger_with_reference(
    guidance_id: str, body: RetriggerWithReferenceRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_reviewer_or_admin)
):
    from app.agents.pipeline import resume_with_reference

    if not body.customer_po and not body.delivery_number:
        raise HTTPException(400, "At least one of customer_po or delivery_number is required")

    g = await db.get(GuidanceQueue, guidance_id)
    if not g:
        raise HTTPException(404)

    # Close the guidance entry
    g.status = "provided"
    g.human_guidance = (
        f"[Retriggered with reference] "
        f"PO={body.customer_po or '—'} | Delivery={body.delivery_number or '—'}"
    )
    g.provided_by = current_user.email
    g.provided_at = datetime.now()

    req = await db.get(EmailRequest, g.request_id)
    if req:
        await log_audit(db, req.id, "guidance_provided",
                        f"Retriggered with reference by {current_user.email}",
                        {"customer_po": body.customer_po, "delivery_number": body.delivery_number},
                        actor=current_user.email)

    await db.commit()
    await resume_with_reference(db, str(g.request_id),
                                customer_po=body.customer_po,
                                delivery_number=body.delivery_number)
    return {"status": "retriggered"}


@router.delete("/bulk")
async def bulk_delete_guidance(
    body: BulkDeleteRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_reviewer_or_admin),
):
    """Soft-delete multiple guidance queue entries. Sets line_deletion_flag=True."""
    if not body.ids:
        raise HTTPException(400, "No IDs provided")
    flagged = 0
    for gid in body.ids:
        try:
            uuid.UUID(gid)
        except (ValueError, AttributeError):
            continue
        g = await db.get(GuidanceQueue, gid)
        if not g:
            continue
        g.line_deletion_flag = True
        req = await db.get(EmailRequest, g.request_id)
        if req:
            req.line_deletion_flag = True
        await log_audit(
            db,
            g.request_id,
            "system",
            f"Guidance queue entry soft-deleted by {current_user.email}",
            {"guidance_id": gid, "reason": g.reason},
            actor=current_user.email,
        )
        flagged += 1
    await db.commit()
    return {"flagged": flagged}


@router.delete("/{guidance_id}")
async def delete_guidance(
    guidance_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_reviewer_or_admin),
):
    """Soft-delete a single guidance queue entry. Sets line_deletion_flag=True."""
    g = await db.get(GuidanceQueue, guidance_id)
    if not g:
        raise HTTPException(404, "Guidance entry not found")
    g.line_deletion_flag = True
    req = await db.get(EmailRequest, g.request_id)
    if req:
        req.line_deletion_flag = True
    await log_audit(
        db,
        g.request_id,
        "system",
        f"Guidance queue entry soft-deleted by {current_user.email}",
        {"guidance_id": guidance_id, "reason": g.reason},
        actor=current_user.email,
    )
    await db.commit()
    return {"deleted": True, "guidance_id": guidance_id}
