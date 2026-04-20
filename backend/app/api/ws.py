from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select, desc, func
import asyncio
import json
from datetime import datetime

from app.db.session import AsyncSessionLocal
from app.models.models import EmailRequest, ApprovalQueue, GuidanceQueue

router = APIRouter()


@router.websocket("/live")
async def websocket_live(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            async with AsyncSessionLocal() as db:
                appr = await db.execute(
                    select(func.count()).select_from(ApprovalQueue)
                    .where(ApprovalQueue.status == "pending")
                    .where(
                        (ApprovalQueue.line_deletion_flag == False) |
                        ApprovalQueue.line_deletion_flag.is_(None)
                    )
                )
                pending_approvals = appr.scalar() or 0

                guid = await db.execute(
                    select(func.count()).select_from(GuidanceQueue)
                    .join(EmailRequest, EmailRequest.id == GuidanceQueue.request_id)
                    .where(GuidanceQueue.status == "pending")
                    .where(
                        (GuidanceQueue.line_deletion_flag == False) |
                        GuidanceQueue.line_deletion_flag.is_(None)
                    )
                    .where(
                        (EmailRequest.line_deletion_flag == False) |
                        EmailRequest.line_deletion_flag.is_(None)
                    )
                )
                pending_guidance = guid.scalar() or 0

                reqs = await db.execute(
                    select(EmailRequest)
                    .where(
                        (EmailRequest.line_deletion_flag == False) |
                        EmailRequest.line_deletion_flag.is_(None)
                    )
                    .order_by(desc(EmailRequest.received_at)).limit(5)
                )
                recent = [{
                    "id": str(r.id),
                    "reference": r.reference_number,
                    "status": r.status,
                    "from_email": r.from_email,
                    "received_at": r.received_at.isoformat() if r.received_at else None,
                } for r in reqs.scalars().all()]

            await websocket.send_text(json.dumps({
                "type": "dashboard_update",
                "pending_approvals": pending_approvals,
                "pending_guidance": pending_guidance,
                "recent_requests": recent,
                "timestamp": datetime.now().isoformat(),
            }, default=str))
            await asyncio.sleep(5)
    except WebSocketDisconnect:
        pass
