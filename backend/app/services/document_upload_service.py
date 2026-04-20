"""
document_upload_service.py
Handles manual upload of POD, packing slip, and invoice documents,
including all cascade updates to related tables and audit logging.
"""
import os
import hashlib
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.models import (
    PodRegistry, PodDocument, PackingSlipDocument, InvoiceDocument,
    EmailRequest, ApprovalQueue,
)
from app.services.audit_service import log_audit
from app.core.config import settings

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB


# ── Validation ────────────────────────────────────────────────

async def _validate_and_convert(file_bytes: bytes, filename: str) -> tuple:
    """
    Validate size and convert to PDF if needed.
    Returns (pdf_bytes, pdf_filename).
    Raises ValueError on size violation, unsupported format, or conversion failure.
    """
    if len(file_bytes) > MAX_UPLOAD_BYTES:
        mb = len(file_bytes) // (1024 * 1024)
        raise ValueError(f"File exceeds the 50 MB limit ({mb} MB uploaded)")

    ext = os.path.splitext(filename)[1].lower()

    if ext == '.pdf':
        if file_bytes[:4] != b'%PDF':
            raise ValueError("File does not appear to be a valid PDF (missing %PDF header)")
        return file_bytes, filename

    # Non-PDF: attempt conversion
    from app.services.pdf_conversion_service import convert_bytes_to_pdf
    return await convert_bytes_to_pdf(file_bytes, filename)


def _safe_name(s: str) -> str:
    """Strip characters unsafe for filenames."""
    return s.replace('/', '-').replace('\\', '-').replace(' ', '_')


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


# ── ApprovalQueue helpers ─────────────────────────────────────

async def _get_pending_approval(
    db: AsyncSession, request_id: str
) -> Optional[ApprovalQueue]:
    r = await db.execute(
        select(ApprovalQueue).where(
            ApprovalQueue.request_id == request_id,
            ApprovalQueue.status == 'pending',
        )
    )
    return r.scalar_one_or_none()


def _rebuild_attachments_json(approval: ApprovalQueue) -> None:
    """Re-derive attachments_json from the three individual filename columns."""
    seen: set = set()
    result = []
    for fname in (approval.draft_attachment,
                  approval.packing_slip_attachment,
                  approval.invoice_attachment):
        if fname and fname not in seen:
            seen.add(fname)
            result.append(fname)
    approval.attachments_json = result if result else None


# ── PodRegistry cascade helper ────────────────────────────────

async def _check_and_promote_registry(
    db: AsyncSession,
    delivery_number: str,
    order_id: Optional[str],
) -> None:
    """
    After a packing slip or invoice upload:
    - Ensure a PodRegistry entry exists for the delivery number (creates pending if missing).
    - If PodRegistry entry is pending/manual_required AND a POD file already exists,
      promote status to have_pod.
    """
    r = await db.execute(
        select(PodRegistry).where(PodRegistry.delivery_number == delivery_number)
    )
    entry = r.scalar_one_or_none()

    if not entry:
        entry = PodRegistry(
            delivery_number=delivery_number,
            status='pending',
            order_id=order_id,
        )
        db.add(entry)
        await db.flush()
        return

    # Link order_id if not already set
    if order_id and not entry.order_id:
        entry.order_id = order_id

    # Promote to have_pod if the POD file is already present
    current_status = str(entry.status)
    if current_status in ('pending', 'manual_required') and entry.filename:
        entry.status = 'have_pod'

    await db.flush()


# ── POD upload ────────────────────────────────────────────────

async def upload_pod(
    db: AsyncSession,
    delivery_number: str,
    file_bytes: bytes,
    original_filename: str,
    order_id: str,
    uploader_email: str,
    request_id: Optional[str] = None,
) -> str:
    """
    Save a manually uploaded POD file and cascade all related updates.

    Cascades:
      - PodRegistry: upsert → status=have_pod, received_via=manual
      - PodDocument: create if none exists for order_id
      - EmailRequest.pod_document_id: re-linked
      - ApprovalQueue.draft_attachment + attachments_json: updated if pending approval
      - If EmailRequest.status == awaiting_pod: enqueues resume_pod_task
      - AuditLog: document_stored action written

    Returns the saved filename.
    Raises ValueError on validation failure.
    """
    file_bytes, original_filename = await _validate_and_convert(file_bytes, original_filename)

    # ── 1. Save file to POD storage ──────────────────────────────
    os.makedirs(settings.POD_STORAGE_PATH, exist_ok=True)
    safe_dn = _safe_name(delivery_number)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    saved_name = f"POD_{safe_dn}_{timestamp}.pdf"
    dest_path = os.path.join(settings.POD_STORAGE_PATH, saved_name)

    with open(dest_path, 'wb') as f:
        f.write(file_bytes)
    logger.info("Manual POD upload saved: %s", dest_path)

    # ── 2. Upsert PodRegistry ────────────────────────────────────
    r = await db.execute(
        select(PodRegistry).where(PodRegistry.delivery_number == delivery_number)
    )
    reg = r.scalar_one_or_none()
    if not reg:
        reg = PodRegistry(delivery_number=delivery_number)
        db.add(reg)

    reg.status = 'have_pod'
    reg.filename = saved_name
    reg.pod_folder_path = dest_path
    reg.received_at = datetime.now()
    reg.received_via = 'manual'
    reg.matched_by = 'manual'
    if order_id:
        reg.order_id = order_id
    await db.flush()

    # ── 3. Upsert PodDocument ────────────────────────────────────
    r = await db.execute(
        select(PodDocument).where(PodDocument.order_id == order_id)
    )
    pod_doc = r.scalars().first()
    if not pod_doc:
        pod_doc = PodDocument(
            order_id=order_id,
            file_name=saved_name,
            file_path=dest_path,
            file_hash=_md5(file_bytes),
            source='manual',
        )
        db.add(pod_doc)
        await db.flush()

    # ── 4. Update EmailRequest + ApprovalQueue ───────────────────
    was_awaiting_pod = False
    if request_id:
        req = await db.get(EmailRequest, request_id)
        if req:
            req.pod_document_id = pod_doc.id
            was_awaiting_pod = str(req.status) == 'awaiting_pod'

            approval = await _get_pending_approval(db, request_id)
            if approval:
                approval.draft_attachment = saved_name
                _rebuild_attachments_json(approval)

            await db.flush()

    # ── 5. Audit log ─────────────────────────────────────────────
    await log_audit(
        db, request_id, "document_stored",
        f"POD manually uploaded for delivery {delivery_number}",
        {
            "doc_type": "pod",
            "delivery_number": delivery_number,
            "filename": saved_name,
            "order_id": str(order_id) if order_id else None,
        },
        actor=uploader_email,
    )

    await db.commit()

    # ── 6. Enqueue resume task AFTER commit ──────────────────────
    # Must be after commit so the worker sees the updated registry row.
    if was_awaiting_pod and request_id:
        from app.core.tasks import resume_pod_task
        resume_pod_task.delay(request_id)
        logger.info("Enqueued resume_pod_task for request %s after manual POD upload", request_id)

    return saved_name


# ── Packing slip upload ───────────────────────────────────────

async def upload_packing_slip(
    db: AsyncSession,
    delivery_number: str,
    order_id: Optional[str],
    file_bytes: bytes,
    original_filename: str,
    uploader_email: str,
    request_id: Optional[str] = None,
) -> str:
    """
    Save a manually uploaded packing slip and cascade all related updates.

    Cascades:
      - PackingSlipDocument: upsert (source=manual)
      - EmailRequest.packing_slip_document_id: re-linked
      - ApprovalQueue.packing_slip_attachment + attachments_json: updated if pending approval
      - PodRegistry: upsert pending entry if missing; promote to have_pod if POD also present
      - AuditLog: document_stored action written

    Returns the saved filename.
    Raises ValueError on validation failure.
    """
    file_bytes, original_filename = await _validate_and_convert(file_bytes, original_filename)

    # ── 1. Save file to packing slips storage ────────────────────
    os.makedirs(settings.PACKING_SLIPS_PATH, exist_ok=True)
    safe_dn = _safe_name(delivery_number)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    saved_name = f"SLIP_{safe_dn}_{timestamp}.pdf"
    dest_path = os.path.join(settings.PACKING_SLIPS_PATH, saved_name)

    with open(dest_path, 'wb') as f:
        f.write(file_bytes)
    logger.info("Manual packing slip upload saved: %s", dest_path)

    # ── 2. Upsert PackingSlipDocument ────────────────────────────
    r = await db.execute(
        select(PackingSlipDocument).where(
            PackingSlipDocument.delivery_number == delivery_number
        )
    )
    slip = r.scalar_one_or_none()
    if not slip:
        slip = PackingSlipDocument(
            order_id=order_id,
            delivery_number=delivery_number,
        )
        db.add(slip)

    slip.order_id = order_id
    slip.file_name = saved_name
    slip.file_path = dest_path
    slip.file_hash = _md5(file_bytes)
    slip.source = 'manual'
    await db.flush()

    # ── 3. Update EmailRequest + ApprovalQueue ───────────────────
    if request_id:
        req = await db.get(EmailRequest, request_id)
        if req:
            req.packing_slip_document_id = slip.id

            approval = await _get_pending_approval(db, request_id)
            if approval:
                approval.packing_slip_attachment = saved_name
                _rebuild_attachments_json(approval)

            await db.flush()

    # ── 4. PodRegistry cascade ───────────────────────────────────
    await _check_and_promote_registry(db, delivery_number, order_id)

    # ── 5. Audit log ─────────────────────────────────────────────
    await log_audit(
        db, request_id, "document_stored",
        f"Packing slip manually uploaded for delivery {delivery_number}",
        {
            "doc_type": "packing_slip",
            "delivery_number": delivery_number,
            "filename": saved_name,
            "order_id": str(order_id) if order_id else None,
        },
        actor=uploader_email,
    )

    await db.commit()
    return saved_name


# ── Invoice upload ────────────────────────────────────────────

async def upload_invoice(
    db: AsyncSession,
    delivery_number: str,
    order_id: Optional[str],
    invoice_number: Optional[str],
    file_bytes: bytes,
    original_filename: str,
    uploader_email: str,
    request_id: Optional[str] = None,
) -> str:
    """
    Save a manually uploaded invoice and cascade all related updates.

    Cascades:
      - InvoiceDocument: upsert (source=manual)
      - EmailRequest.invoice_document_id: re-linked
      - ApprovalQueue.invoice_attachment + attachments_json: updated if pending approval
      - PodRegistry: upsert pending entry if missing; promote to have_pod if POD also present
      - AuditLog: document_stored action written

    Returns the saved filename.
    Raises ValueError on validation failure.
    """
    file_bytes, original_filename = await _validate_and_convert(file_bytes, original_filename)

    # ── 1. Save file to invoices storage ─────────────────────────
    os.makedirs(settings.INVOICES_PATH, exist_ok=True)
    safe_ref = _safe_name(invoice_number or delivery_number)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    saved_name = f"INV_{safe_ref}_{timestamp}.pdf"
    dest_path = os.path.join(settings.INVOICES_PATH, saved_name)

    with open(dest_path, 'wb') as f:
        f.write(file_bytes)
    logger.info("Manual invoice upload saved: %s", dest_path)

    # ── 2. Upsert InvoiceDocument ────────────────────────────────
    r = await db.execute(
        select(InvoiceDocument).where(InvoiceDocument.order_id == order_id)
    )
    inv = r.scalars().first()
    if not inv:
        inv = InvoiceDocument(order_id=order_id)
        db.add(inv)

    inv.order_id = order_id
    inv.invoice_number = invoice_number
    inv.file_name = saved_name
    inv.file_path = dest_path
    inv.file_hash = _md5(file_bytes)
    inv.source = 'manual'
    await db.flush()

    # ── 3. Update EmailRequest + ApprovalQueue ───────────────────
    if request_id:
        req = await db.get(EmailRequest, request_id)
        if req:
            req.invoice_document_id = inv.id

            approval = await _get_pending_approval(db, request_id)
            if approval:
                approval.invoice_attachment = saved_name
                _rebuild_attachments_json(approval)

            await db.flush()

    # ── 4. PodRegistry cascade ───────────────────────────────────
    await _check_and_promote_registry(db, delivery_number, order_id)

    # ── 5. Audit log ─────────────────────────────────────────────
    await log_audit(
        db, request_id, "document_stored",
        f"Invoice manually uploaded for delivery {delivery_number}",
        {
            "doc_type": "invoice",
            "delivery_number": delivery_number,
            "invoice_number": invoice_number,
            "filename": saved_name,
            "order_id": str(order_id) if order_id else None,
        },
        actor=uploader_email,
    )

    await db.commit()
    return saved_name
