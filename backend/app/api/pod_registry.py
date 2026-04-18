from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, String
from pydantic import BaseModel
from typing import Optional
import tempfile, os
from datetime import datetime, timezone

from app.db.session import get_db
from app.models.models import PodRegistry, Carrier, Order, User, SystemConfig
from app.core.security import require_admin, get_current_user, require_reviewer_or_admin
from app.services.pod_folder_service import (
    list_registry, save_pod_bytes, mark_pod_requested, check_registry, get_pod_folder
)
from app.services.email_service import send_pod_request_to_carrier
from app.services.audit_service import log_audit
from app.services.ftp_service import poll_ftp
from app.core.config import settings

router = APIRouter()


class RegistryEntryUpdate(BaseModel):
    notes: Optional[str] = None
    status: Optional[str] = None
    delivery_number: Optional[str] = None
    customer_po: Optional[str] = None
    carrier_id: Optional[str] = None


class RequestPodBody(BaseModel):
    delivery_numbers: list[str]
    carrier_id: str  # UUID or the special value "default"
    customer_po: Optional[str] = None


class LinkOrderBody(BaseModel):
    order_id: str


@router.get("")
async def get_registry(
    status: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await list_registry(db, status=status, search=search, limit=500 if search else limit, offset=offset)


@router.get("/integrity-check")
async def registry_integrity_check(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    """
    Check that every pod_registry entry is properly linked to the orders table
    using delivery_number as the primary key.

    Flags entries where:
    - delivery_number does not match any orders.my_delivery_number  (primary check)
    - order_id FK is missing or points to a non-existent order      (secondary check)
    """
    result = await db.execute(select(PodRegistry).where(PodRegistry.is_deleted == False))
    entries = result.scalars().all()

    # Build a set of all known delivery numbers from the orders table for O(1) lookup
    dn_result = await db.execute(
        select(Order.my_delivery_number, Order.id, Order.customer_order_number)
    )
    orders_by_dn = {row[0]: {"id": str(row[1]), "customer_order_number": row[2]}
                   for row in dn_result.all() if row[0]}

    issues = []
    for e in entries:
        entry_issues = []

        # ── Primary check: delivery_number must exist in orders.my_delivery_number
        if not e.delivery_number:
            entry_issues.append("registry entry has no delivery_number")
        elif e.delivery_number not in orders_by_dn:
            entry_issues.append(
                f"delivery_number '{e.delivery_number}' not found in orders.my_delivery_number"
            )

        # ── Secondary check: order_id must be set and point to a valid order
        if not e.order_id:
            entry_issues.append("order_id not linked")
        else:
            order = await db.get(Order, e.order_id)
            if not order:
                entry_issues.append(f"order_id {e.order_id} not found in orders table")

        if entry_issues:
            issues.append({
                "id": str(e.id),
                "delivery_number": e.delivery_number,
                "customer_po": e.customer_po,
                "status": str(e.status),
                "issues": entry_issues,
            })

    healthy_count = len(entries) - len(issues)
    return {
        "total": len(entries),
        "healthy": healthy_count,
        "issues_found": len(issues),
        "all_healthy": len(issues) == 0,
        "entries_with_issues": issues,
    }


@router.get("/stats")
async def registry_stats(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    from sqlalchemy import func
    result = await db.execute(
        select(PodRegistry.status.cast(String), func.count())
        .where(PodRegistry.is_deleted == False)
        .group_by(PodRegistry.status)
    )
    counts = {row[0]: row[1] for row in result.all()}
    total = sum(counts.values())
    return {
        'total': total,
        'have_pod': counts.get('have_pod', 0),
        'requested': counts.get('requested', 0),
        'pending': counts.get('pending', 0),
        'failed': counts.get('failed', 0),
        'manual_required': counts.get('manual_required', 0),
    }


@router.get("/folder")
async def get_folder_info(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    folder = await get_pod_folder(db)
    try:
        files = [f for f in os.listdir(folder) if f.endswith('.pdf')]
    except Exception:
        files = []
    return {'path': folder, 'pdf_count': len(files), 'files': files[:50]}


@router.get("/packing-slips/stats")
async def packing_slip_stats(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    from sqlalchemy import func
    from app.models.models import PackingSlipDocument
    total  = await db.scalar(select(func.count()).select_from(PackingSlipDocument)) or 0
    linked = await db.scalar(
        select(func.count()).select_from(PackingSlipDocument)
        .where(PackingSlipDocument.order_id.isnot(None))
    ) or 0
    # file_path NULL means record was created without a stored file
    file_ok = await db.scalar(
        select(func.count()).select_from(PackingSlipDocument)
        .where(PackingSlipDocument.file_path.isnot(None))
    ) or 0
    return {
        'total': total, 'linked': linked, 'unlinked': total - linked,
        'file_ok': file_ok, 'file_missing': total - file_ok,
    }


@router.get("/packing-slips")
async def list_packing_slips(
    limit: int = 200,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    from app.models.models import PackingSlipDocument
    result = await db.execute(
        select(PackingSlipDocument)
        .order_by(PackingSlipDocument.created_at.desc())
        .limit(limit).offset(offset)
    )
    return [_slip_out(d) for d in result.scalars().all()]


@router.get("/invoices/stats")
async def invoice_stats(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    from sqlalchemy import func
    from app.models.models import InvoiceDocument
    total  = await db.scalar(select(func.count()).select_from(InvoiceDocument)) or 0
    linked = await db.scalar(
        select(func.count()).select_from(InvoiceDocument)
        .where(InvoiceDocument.order_id.isnot(None))
    ) or 0
    file_ok = await db.scalar(
        select(func.count()).select_from(InvoiceDocument)
        .where(InvoiceDocument.file_path.isnot(None))
    ) or 0
    return {
        'total': total, 'linked': linked, 'unlinked': total - linked,
        'file_ok': file_ok, 'file_missing': total - file_ok,
    }


@router.get("/invoices")
async def list_invoices(
    limit: int = 200,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    from app.models.models import InvoiceDocument
    result = await db.execute(
        select(InvoiceDocument)
        .order_by(InvoiceDocument.created_at.desc())
        .limit(limit).offset(offset)
    )
    return [_inv_out(d) for d in result.scalars().all()]


def _slip_out(d) -> dict:
    # Use file_path presence as proxy — avoids 1M os.path.isfile() syscalls in bulk lists.
    # Actual disk check only happens at download time.
    return {
        'id':              str(d.id),
        'order_id':        str(d.order_id) if d.order_id else None,
        'delivery_number': d.delivery_number,
        'file_name':       d.file_name,
        'file_hash':       d.file_hash,
        'source':          d.source,
        'file_exists':     bool(d.file_path),
        'created_at':      d.created_at.isoformat() if d.created_at else None,
    }


def _inv_out(d) -> dict:
    return {
        'id':             str(d.id),
        'order_id':       str(d.order_id) if d.order_id else None,
        'invoice_number': d.invoice_number,
        'file_name':      d.file_name,
        'file_hash':      d.file_hash,
        'source':         d.source,
        'file_exists':    bool(d.file_path),
        'created_at':     d.created_at.isoformat() if d.created_at else None,
    }


@router.post("/{registry_id}/link-order")
async def link_order_to_registry(
    registry_id: str,
    body: LinkOrderBody,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """
    Back-fill order_id on a PodRegistry entry and all related document records
    (PodDocument, PackingSlipDocument, InvoiceDocument) that share the same
    delivery_number and currently have order_id = NULL.
    Requires admin role.
    """
    from app.models.models import PodDocument, PackingSlipDocument, InvoiceDocument
    from app.services.audit_service import log_audit

    order_id = body.order_id

    reg = await db.get(PodRegistry, registry_id)
    if not reg:
        raise HTTPException(404, "Registry entry not found")

    order = await db.get(Order, order_id)
    if not order:
        raise HTTPException(404, "Order not found")

    dn = reg.delivery_number
    updated = []

    # ── 1. PodRegistry ──────────────────────────────────────────
    reg.order_id = order_id
    reg.customer_po = order.customer_order_number  # always sync to the chosen order
    updated.append("pod_registry")

    # ── 2. PodDocument (matched by filename stored in registry) ──
    if reg.filename:
        r = await db.execute(
            select(PodDocument).where(
                PodDocument.file_name == reg.filename,
                PodDocument.order_id.is_(None),
            )
        )
        for pod_doc in r.scalars().all():
            pod_doc.order_id = order_id
            updated.append(f"pod_document:{pod_doc.id}")

    # ── 3. PackingSlipDocument (matched by delivery_number) ──────
    r = await db.execute(
        select(PackingSlipDocument).where(
            PackingSlipDocument.delivery_number == dn,
            PackingSlipDocument.order_id.is_(None),
        )
    )
    for slip in r.scalars().all():
        slip.order_id = order_id
        updated.append(f"packing_slip:{slip.id}")

    # ── 4. InvoiceDocument (matched by invoice_number if available) ──
    if order.invoice_number:
        r = await db.execute(
            select(InvoiceDocument).where(
                InvoiceDocument.invoice_number == order.invoice_number,
                InvoiceDocument.order_id.is_(None),
            )
        )
        for inv in r.scalars().all():
            inv.order_id = order_id
            updated.append(f"invoice:{inv.id}")

    await log_audit(
        db, None, "system",
        f"Order {order.customer_order_number} linked to delivery {dn} by {current_user.email}",
        {
            "registry_id": registry_id,
            "delivery_number": dn,
            "order_id": order_id,
            "order": order.customer_order_number,
            "records_updated": updated,
        },
        actor=current_user.email,
    )

    await db.commit()
    return {
        "linked": True,
        "delivery_number": dn,
        "order_id": order_id,
        "order": order.customer_order_number,
        "records_updated": updated,
    }


@router.get("/{registry_id}")
async def get_registry_entry(
    registry_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    entry = await db.get(PodRegistry, registry_id)
    if not entry:
        raise HTTPException(404)
    from app.services.pod_folder_service import _reg_out
    return _reg_out(entry)


@router.delete("/{registry_id}")
async def delete_registry_entry(
    registry_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Soft-delete a registry entry. Sets is_deleted=True; row is hidden from all views and reports. Admin only."""
    from app.services.audit_service import log_audit
    entry = await db.get(PodRegistry, registry_id)
    if not entry:
        raise HTTPException(404, "Registry entry not found")
    dn = entry.delivery_number
    entry.is_deleted = True
    entry.deleted_at = datetime.now(timezone.utc)
    await log_audit(
        db, None, "system",
        f"Registry entry soft-deleted for delivery {dn} by {current_user.email}",
        {"registry_id": registry_id, "delivery_number": dn, "status": str(entry.status)},
        actor=current_user.email,
    )
    await db.commit()
    return {"deleted": True, "delivery_number": dn}


@router.put("/{registry_id}")
async def update_registry_entry(
    registry_id: str,
    data: RegistryEntryUpdate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_reviewer_or_admin),
):
    entry = await db.get(PodRegistry, registry_id)
    if not entry:
        raise HTTPException(404)
    for k, v in data.model_dump(exclude_none=True).items():
        setattr(entry, k, v)
    await db.commit()
    from app.services.pod_folder_service import _reg_out
    return _reg_out(entry)


@router.post("/request-pods")
async def request_pods_from_carrier(
    body: RequestPodBody,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_reviewer_or_admin),
):
    """Send a POD request email to a carrier for specified delivery numbers."""
    if body.carrier_id == "default":
        cfg = await db.get(SystemConfig, "default_pod_request_email")
        if not cfg or not cfg.value:
            raise HTTPException(400, "No default POD request email configured in system settings")
        carrier_name = "Default"
        carrier_email = cfg.value
        carrier_id_str = "default"
    else:
        carrier = await db.get(Carrier, body.carrier_id)
        if not carrier:
            raise HTTPException(404, "Carrier not found")
        if not carrier.email:
            raise HTTPException(400, f"Carrier {carrier.name} has no email configured")
        carrier_name = carrier.name
        carrier_email = carrier.email
        carrier_id_str = str(carrier.id)

    result = await send_pod_request_to_carrier(
        db=db,
        carrier_name=carrier_name,
        carrier_email=carrier_email,
        delivery_numbers=body.delivery_numbers,
        customer_po=body.customer_po,
    )

    sent_ok = result.get("sent", False)
    await log_audit(
        db, None, "email_sent",
        f"POD request email {'sent' if sent_ok else 'FAILED'} to {carrier_name} <{carrier_email}>",
        {
            "to": carrier_email,
            "carrier": carrier_name,
            "delivery_numbers": body.delivery_numbers,
            "customer_po": body.customer_po,
            "message_id": result.get("message_id"),
            "error": result.get("error"),
        },
        actor="system",
        success=sent_ok,
        error_detail=result.get("error"),
    )
    await db.commit()

    if sent_ok:
        for dn in body.delivery_numbers:
            await mark_pod_requested(
                db=db,
                delivery_number=dn,
                message_id=result.get('message_id'),
                carrier_id=carrier_id_str,
                customer_po=body.customer_po,
            )

    return result


@router.post("/upload")
async def manual_upload_pod(
    delivery_number: str = Form(...),
    file: UploadFile = File(...),
    customer_po: Optional[str] = Form(None),
    carrier_id: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_reviewer_or_admin),
):
    """Manually upload a POD file and link it to a delivery number."""
    file_bytes = await file.read()
    saved = await save_pod_bytes(
        db=db,
        file_bytes=file_bytes,
        delivery_number=delivery_number,
        original_filename=file.filename,
        received_via='manual',
        matched_by='manual',
        carrier_id=carrier_id,
        customer_po=customer_po,
    )
    if not saved:
        raise HTTPException(500, "Failed to save POD file")
    await log_audit(
        db, None, "pod_generated",
        f"Manual POD upload for delivery {delivery_number} by {current_user.email}",
        {"delivery_number": delivery_number, "filename": saved, "original": file.filename,
         "customer_po": customer_po, "carrier_id": carrier_id},
        actor=current_user.email,
    )
    await db.commit()
    return {'saved': True, 'filename': saved, 'delivery_number': delivery_number}


@router.post("/poll-ftp")
async def trigger_ftp_poll(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Manually trigger an FTP poll (admin only)."""
    url_row = await db.get(SystemConfig, "ollama_base_url")
    model_row = await db.get(SystemConfig, "ollama_model")
    ollama_url = (url_row.value.strip() if url_row and url_row.value else None) or settings.OLLAMA_BASE_URL
    ollama_model = (model_row.value.strip() if model_row and model_row.value else None) or settings.OLLAMA_MODEL
    result = await poll_ftp(db, ollama_url, ollama_model)
    return result


@router.get("/download/{registry_id}")
async def download_pod(
    registry_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    from fastapi.responses import FileResponse
    entry = await db.get(PodRegistry, registry_id)
    if not entry or not entry.filename:
        raise HTTPException(404, "POD file not found")

    # pod_folder_path may store: full file path, directory only, or be null.
    if entry.pod_folder_path:
        if os.path.isdir(entry.pod_folder_path):
            # Directory stored — join with filename
            file_path = os.path.join(entry.pod_folder_path, entry.filename)
        else:
            # Full file path stored
            file_path = entry.pod_folder_path
    else:
        # No path stored — look in default POD storage folder
        file_path = os.path.join(settings.POD_STORAGE_PATH, entry.filename)

    if not os.path.exists(file_path):
        raise HTTPException(404, "POD file missing from disk")
    return FileResponse(
        file_path,
        media_type='application/pdf',
        filename=entry.filename or 'pod.pdf'
    )
