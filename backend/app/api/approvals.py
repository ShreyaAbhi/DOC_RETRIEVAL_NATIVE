from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, String
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
import os

from app.db.session import get_db
from app.models.models import ApprovalQueue, EmailRequest, User
from app.services.audit_service import log_audit
from app.services.email_service import send_email
from app.core.config import settings
from app.core.security import get_current_user, require_reviewer_or_admin

router = APIRouter()


class ApprovalAction(BaseModel):
    action: str  # "approve" | "reject"
    reviewer: Optional[str] = "admin"
    notes: Optional[str] = None
    modified_body: Optional[str] = None


@router.get("")
async def list_approvals(status: Optional[str] = "pending", db: AsyncSession = Depends(get_db),
                          _: User = Depends(require_reviewer_or_admin)):
    q = (
        select(ApprovalQueue)
        .where(
            (ApprovalQueue.line_deletion_flag == False) |
            ApprovalQueue.line_deletion_flag.is_(None)
        )
        .order_by(desc(ApprovalQueue.created_at))
    )
    if status:
        q = q.where(ApprovalQueue.status.cast(String) == status)
    result = await db.execute(q)
    out = []
    for a in result.scalars().all():
        req = await db.get(EmailRequest, a.request_id)
        out.append({
            "id": str(a.id),
            "request_id": str(a.request_id),
            "status": a.status,
            "draft_subject": a.draft_subject,
            "draft_body": a.draft_body,
            "draft_attachment": a.draft_attachment,
            "packing_slip_attachment": a.packing_slip_attachment,
            "invoice_attachment": a.invoice_attachment,
            "attachments_json": a.attachments_json or [],
            "reviewer_notes": a.reviewer_notes,
            "expires_at": a.expires_at.isoformat() if a.expires_at else None,
            "created_at": a.created_at.isoformat() if a.created_at else None,
            "request": {
                "from_email": req.from_email,
                "subject": req.subject,
                "reference_number": req.reference_number,
                "extracted_order_id": req.extracted_order_id,
                "confidence_score": float(req.confidence_score) if req.confidence_score else None,
                "intent": req.intent,
            } if req else None,
        })
    return out


@router.post("/{approval_id}/action")
async def action_approval(
    approval_id: str, body: ApprovalAction, db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_reviewer_or_admin)
):
    appr = await db.get(ApprovalQueue, approval_id)
    if not appr:
        raise HTTPException(404)
    if appr.status != "pending":
        raise HTTPException(400, "Already actioned")

    appr.status     = "approved" if body.action == "approve" else "rejected"
    appr.reviewed_by  = body.reviewer
    appr.reviewed_at  = datetime.utcnow()
    appr.reviewer_notes = body.notes

    req = await db.get(EmailRequest, appr.request_id)
    if req:
        if body.action == "approve":
            if body.modified_body:
                req.response_body = body.modified_body

            # Build attachment list from stored file paths.
            # attachments_json now stores full file paths; fall back to
            # directory search for legacy entries that only stored filenames.
            all_search_dirs = [
                settings.POD_STORAGE_PATH,
                settings.DOCUMENTS_PATH,
                settings.PACKING_SLIPS_PATH,
                settings.INVOICES_PATH,
            ]
            entries = (appr.attachments_json or []) or [
                f for f in [appr.draft_attachment,
                             appr.packing_slip_attachment,
                             appr.invoice_attachment]
                if f
            ]
            attachments = []
            for entry in entries:
                if not entry:
                    continue
                # If the entry is a full path that exists, use it directly
                if os.path.exists(entry):
                    attachments.append(entry)
                    continue
                # Fallback: search storage directories (legacy filename-only entries)
                for folder in all_search_dirs:
                    candidate = os.path.join(folder, entry)
                    if os.path.exists(candidate):
                        attachments.append(candidate)
                        break

            email_subject = appr.draft_subject or f"Re: {req.subject}"
            email_body = req.response_body or appr.draft_body or ""
            send_result = await send_email(
                db,
                to=req.from_email,
                subject=email_subject,
                body=email_body,
                attachments=attachments or None,
            )

            req.status = "completed"
            req.response_sent_at = datetime.utcnow()
            req.completed_at     = datetime.utcnow()
            if send_result.get("message_id"):
                req.smtp_message_id = send_result["message_id"]
            await log_audit(db, req.id, "approved",
                            f"Response approved by {body.reviewer}",
                            {"reviewer": body.reviewer, "notes": body.notes},
                            actor=body.reviewer)

            # Dedicated email_sent audit entry
            sent_ok = send_result.get("sent", False)
            await log_audit(
                db, req.id, "email_sent",
                f"Response email {'sent' if sent_ok else 'FAILED'} to {req.from_email}",
                {
                    "to": req.from_email,
                    "subject": email_subject,
                    "attachments": [os.path.basename(a) for a in attachments],
                    "message_id": send_result.get("message_id"),
                    "error": send_result.get("error"),
                },
                actor=body.reviewer,
                success=sent_ok,
                error_detail=send_result.get("error"),
            )
        else:
            req.status = "rejected"
            req.line_deletion_flag = True
            appr.line_deletion_flag = True
            await log_audit(db, req.id, "rejected",
                            f"Response rejected by {body.reviewer}",
                            {"reason": body.notes}, actor=body.reviewer)

    await db.commit()
    return {"status": appr.status}
