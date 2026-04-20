from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from datetime import datetime
from typing import Optional

from app.db.session import get_db
from app.models.models import SystemConfig, User
from app.core.security import require_admin
from app.services.autopoll_service import get_autopoll_config, run_autopoll

router = APIRouter()

_KEY_ENABLED = "autopoll_enabled"
_KEY_PATH    = "autopoll_path"
_KEY_FREQ    = "autopoll_frequency_minutes"


class AutopollConfig(BaseModel):
    enabled: bool
    path: str
    frequency_minutes: int


@router.get("/status")
async def autopoll_status(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    return await get_autopoll_config(db)


@router.put("/config")
async def update_autopoll_config(
    body: AutopollConfig,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    updates = {
        _KEY_ENABLED: "true" if body.enabled else "false",
        _KEY_PATH:    body.path,
        _KEY_FREQ:    str(body.frequency_minutes),
    }
    for key, value in updates.items():
        cfg = await db.get(SystemConfig, key)
        if not cfg:
            cfg = SystemConfig(key=key)
            db.add(cfg)
        cfg.value = value
        cfg.updated_at = datetime.now()
    await db.commit()
    return await get_autopoll_config(db)


@router.post("/trigger")
async def trigger_autopoll(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Immediately run a poll cycle regardless of the configured interval."""
    result = await run_autopoll(db)
    return result


@router.post("/preread-documents")
async def preread_documents(
    _: User = Depends(require_admin),
):
    """
    Queue a pre-read pass over all storage folders.
    Extracts text from each file, finds a matching reference number from the
    orders table, and renames the file by prepending the reference so the
    subsequent scan step can match it by filename.
    """
    from app.core.tasks import preread_documents_task
    preread_documents_task.delay()
    return {"message": "Document pre-read queued. Files will be renamed within seconds."}


@router.post("/scan-documents")
async def scan_all_documents(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    """
    Queue a document scan for every order in the database.
    Looks in pod_storage, packing_slips, and invoices folders for matching files.
    """
    from app.models.models import Order
    result = await db.execute(select(Order.id))
    all_ids = [str(row[0]) for row in result.all()]
    if not all_ids:
        return {"queued": 0, "message": "No orders found"}
    from app.core.tasks import scan_order_documents_task
    scan_order_documents_task.delay(all_ids)
    return {"queued": len(all_ids), "message": f"Scan queued for {len(all_ids)} order(s)"}
