"""
pod_folder_service.py
Handles all interactions with the POD storage folder and registry.
"""
import os
import shutil
import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Tuple
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.models import PodRegistry, Carrier, Order, SystemConfig, PodDocument
from app.core.config import settings

logger = logging.getLogger(__name__)


async def get_pod_folder(db: AsyncSession) -> str:
    """Get configured POD folder path, fallback to default."""
    result = await db.execute(
        select(SystemConfig).where(SystemConfig.key == 'pod_folder_path')
    )
    cfg = result.scalar_one_or_none()
    path = cfg.value if cfg and cfg.value else settings.POD_STORAGE_PATH
    os.makedirs(path, exist_ok=True)
    return path


async def check_registry(
    db: AsyncSession,
    delivery_numbers: List[str]
) -> Tuple[List[dict], List[str]]:
    """
    Check which delivery numbers already have PODs and which are missing.
    Returns (have_pod_list, missing_list)
    """
    have = []
    missing = []
    for dn in delivery_numbers:
        result = await db.execute(
            select(PodRegistry).where(PodRegistry.delivery_number == dn)
        )
        entry = result.scalar_one_or_none()
        if entry and str(entry.status) == 'have_pod' and entry.filename:
            have.append({
                'delivery_number': dn,
                'filename': entry.filename,
                'pod_folder_path': entry.pod_folder_path,
                'received_at': entry.received_at,
                'received_via': str(entry.received_via) if entry.received_via else None,
            })
        else:
            missing.append(dn)
            # Ensure a registry entry exists for tracking
            if not entry:
                new_entry = PodRegistry(
                    delivery_number=dn,
                    status='pending',
                )
                db.add(new_entry)
    await db.commit()
    return have, missing


async def save_pod_file(
    db: AsyncSession,
    source_path: str,
    delivery_number: str,
    original_filename: str,
    received_via: str = 'email',
    matched_by: str = 'ai',
    carrier_id: Optional[str] = None,
    order_id: Optional[str] = None,
    customer_po: Optional[str] = None,
) -> Optional[str]:
    """
    Copy a POD file into the configured POD folder and update the registry.
    Returns the saved filename, or None on failure.
    """
    try:
        pod_folder = await get_pod_folder(db)
        ext = os.path.splitext(original_filename)[1] or '.pdf'
        safe_dn = delivery_number.replace('/', '-').replace('\\', '-')
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        dest_filename = f"POD_{safe_dn}_{timestamp}{ext}"
        dest_path = os.path.join(pod_folder, dest_filename)

        shutil.copy2(source_path, dest_path)
        logger.info(f"Saved POD to {dest_path}")

        # Update registry
        result = await db.execute(
            select(PodRegistry).where(PodRegistry.delivery_number == delivery_number)
        )
        entry = result.scalar_one_or_none()
        if not entry:
            entry = PodRegistry(delivery_number=delivery_number)
            db.add(entry)

        entry.status = 'have_pod'
        entry.filename = dest_filename
        entry.pod_folder_path = dest_path
        entry.received_at = datetime.now()
        entry.received_via = received_via
        entry.matched_by = matched_by
        if carrier_id:
            entry.carrier_id = carrier_id
        if order_id:
            entry.order_id = order_id
        if customer_po:
            entry.customer_po = customer_po

        await db.commit()
        return dest_filename

    except Exception as e:
        logger.error(f"Failed to save POD file: {e}")
        return None


async def save_pod_bytes(
    db: AsyncSession,
    file_bytes: bytes,
    delivery_number: str,
    original_filename: str,
    received_via: str = 'email',
    matched_by: str = 'ai',
    carrier_id: Optional[str] = None,
    order_id: Optional[str] = None,
    customer_po: Optional[str] = None,
) -> Optional[str]:
    """Save raw PDF bytes directly to the POD folder and update registry."""
    try:
        pod_folder = await get_pod_folder(db)
        ext = os.path.splitext(original_filename)[1] or '.pdf'
        safe_dn = delivery_number.replace('/', '-').replace('\\', '-')
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        dest_filename = f"POD_{safe_dn}_{timestamp}{ext}"
        dest_path = os.path.join(pod_folder, dest_filename)

        with open(dest_path, 'wb') as f:
            f.write(file_bytes)
        logger.info(f"Saved POD bytes to {dest_path}")

        result = await db.execute(
            select(PodRegistry).where(PodRegistry.delivery_number == delivery_number)
        )
        entry = result.scalar_one_or_none()
        if not entry:
            entry = PodRegistry(delivery_number=delivery_number)
            db.add(entry)

        entry.status = 'have_pod'
        entry.filename = dest_filename
        entry.pod_folder_path = dest_path
        entry.received_at = datetime.now()
        entry.received_via = received_via
        entry.matched_by = matched_by
        if carrier_id:
            entry.carrier_id = carrier_id
        # Validate and resolve order_id — must exist in orders table
        resolved = await _resolve_order_id(db, order_id, customer_po, delivery_number)
        if resolved:
            entry.order_id = resolved
        if customer_po:
            entry.customer_po = customer_po

        await db.commit()
        return dest_filename

    except Exception as e:
        logger.error(f"Failed to save POD bytes: {e}")
        return None


async def _resolve_order_id(db: AsyncSession, order_id: Optional[str],
                             customer_po: Optional[str],
                             delivery_number: Optional[str]) -> Optional[str]:
    """
    Resolve a valid order UUID. Validates that the order exists; if not found,
    tries to match by customer_order_number using customer_po or delivery_number.
    Returns the order UUID string or None.
    """
    from app.models.models import Order
    from sqlalchemy import or_
    if order_id:
        order = await db.get(Order, order_id)
        if order:
            return str(order.id)
        logger.warning("order_id %s not found in orders table — will search by PO/delivery", order_id)

    # Try to find by customer_order_number or delivery number
    search_values = [v for v in [customer_po, delivery_number] if v]
    if search_values:
        r = await db.execute(
            select(Order).where(
                or_(
                    Order.customer_order_number.in_(search_values),
                    Order.my_delivery_number.in_(search_values),
                )
            )
        )
        order = r.scalars().first()
        if order:
            return str(order.id)

    logger.warning(
        "No matching order found for customer_po=%s delivery=%s — "
        "registry entry will be created without order link",
        customer_po, delivery_number
    )
    return None


async def mark_pod_requested(
    db: AsyncSession,
    delivery_number: str,
    message_id: Optional[str] = None,
    carrier_id: Optional[str] = None,
    order_id: Optional[str] = None,
    customer_po: Optional[str] = None,
):
    """Mark a delivery number as having been requested from carrier."""
    result = await db.execute(
        select(PodRegistry).where(PodRegistry.delivery_number == delivery_number)
    )
    entry = result.scalar_one_or_none()
    if not entry:
        entry = PodRegistry(delivery_number=delivery_number)
        db.add(entry)

    entry.status = 'requested'
    entry.requested_at = datetime.now()
    if message_id:
        entry.request_email_message_id = message_id
    if carrier_id:
        entry.carrier_id = carrier_id
    # Validate and resolve order_id — must exist in orders table
    resolved = await _resolve_order_id(db, order_id, customer_po, delivery_number)
    if resolved:
        entry.order_id = resolved
    if customer_po:
        entry.customer_po = customer_po

    await db.commit()


async def get_registry_for_order(db: AsyncSession, order_id: str) -> List[dict]:
    """Get all POD registry entries for an order."""
    result = await db.execute(
        select(PodRegistry).where(PodRegistry.order_id == order_id)
    )
    entries = result.scalars().all()
    return [_reg_out(e) for e in entries]


async def list_registry(
    db: AsyncSession,
    status: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> List[dict]:
    from app.models.models import PackingSlipDocument, InvoiceDocument
    from sqlalchemy import or_

    q = select(PodRegistry).where(PodRegistry.is_deleted == False).order_by(PodRegistry.created_at.desc())
    if status:
        q = q.where(PodRegistry.status.cast(__import__('sqlalchemy').String) == status)
    if search:
        term = f"%{search}%"
        q = q.where(or_(
            PodRegistry.delivery_number.ilike(term),
            PodRegistry.customer_po.ilike(term),
            PodRegistry.status.cast(__import__('sqlalchemy').String).ilike(term),
            PodRegistry.filename.ilike(term),
            PodRegistry.received_via.cast(__import__('sqlalchemy').String).ilike(term),
            PodRegistry.notes.ilike(term),
        ))
    q = q.limit(limit).offset(offset)
    result = await db.execute(q)
    entries = result.scalars().all()

    if not entries:
        return []

    delivery_numbers = [e.delivery_number for e in entries if e.delivery_number]
    order_ids        = [e.order_id for e in entries if e.order_id]

    # Packing slips keyed by delivery_number and order_id
    slip_by_dn    = {}
    slip_by_order = {}
    if delivery_numbers:
        r = await db.execute(select(PackingSlipDocument).where(PackingSlipDocument.delivery_number.in_(delivery_numbers)))
        for s in r.scalars().all():
            slip_by_dn.setdefault(s.delivery_number, s)
    if order_ids:
        r = await db.execute(select(PackingSlipDocument).where(PackingSlipDocument.order_id.in_(order_ids)))
        for s in r.scalars().all():
            slip_by_order.setdefault(str(s.order_id), s)

    # Invoices keyed by order_id
    inv_by_order = {}
    if order_ids:
        r = await db.execute(select(InvoiceDocument).where(InvoiceDocument.order_id.in_(order_ids)))
        for i in r.scalars().all():
            inv_by_order.setdefault(str(i.order_id), i)

    out = []
    for e in entries:
        d = _reg_out(e)
        slip = slip_by_dn.get(e.delivery_number) or (slip_by_order.get(str(e.order_id)) if e.order_id else None)
        inv  = inv_by_order.get(str(e.order_id)) if e.order_id else None
        d['packing_slip_status']    = 'have_slip' if slip else 'missing'
        d['packing_slip_file_name'] = slip.file_name if slip else None
        d['invoice_status']         = 'have_invoice' if inv else 'missing'
        d['invoice_file_name']      = inv.file_name if inv else None
        out.append(d)
    return out


async def scan_pod_folder_for_order(
    db: AsyncSession,
    order: Order,
) -> Optional[PodDocument]:
    """
    Scan the configured POD storage folder for a PDF whose filename contains
    the order's delivery number.  Used after autopoll imports to find PODs that
    were manually dropped into pod_storage before or after the order was created.

    Guards:
    - Returns None if the order has no my_delivery_number (nothing to match).
    - Returns None if pod_registry already shows have_pod (already registered).

    On a match:
    - Updates the pod_registry entry to have_pod.
    - Creates a PodDocument record pointing at the file.
    - Does NOT copy/move the file — it is already in the correct folder.
    """
    if not order or not order.my_delivery_number:
        return None

    delivery = order.my_delivery_number

    # Guard: already registered?
    reg_result = await db.execute(
        select(PodRegistry).where(PodRegistry.delivery_number == delivery)
    )
    reg = reg_result.scalar_one_or_none()
    if reg and str(reg.status) == 'have_pod' and reg.filename:
        return None

    pod_folder = await get_pod_folder(db)
    folder = Path(pod_folder)
    if not folder.exists():
        return None

    from app.services.pdf_conversion_service import SCANNABLE_EXTENSIONS, convert_to_pdf

    def _n(s): return (s or "").replace("-", "").replace("_", "").replace(" ", "").lower()

    search_norms = [n for n in [
        _n(delivery),
        _n(order.invoice_number),
        _n(order.customer_order_number),
    ] if n]

    candidates = sorted(
        f for f in folder.iterdir()
        if f.is_file() and f.suffix.lower() in SCANNABLE_EXTENSIONS
    )
    for f in candidates:
        fname_norm = _n(f.name)
        if any(ref in fname_norm for ref in search_norms):
            # Convert to PDF if needed
            pdf_path_str = await convert_to_pdf(str(f))
            if not pdf_path_str:
                continue
            pdf_f = Path(pdf_path_str)

            # Check if a PodDocument already points at this file
            existing_doc = await db.execute(
                select(PodDocument).where(PodDocument.file_name == pdf_f.name)
            )
            pod = existing_doc.scalar_one_or_none()
            if pod:
                if not pod.order_id:
                    pod.order_id = order.id
                await db.flush()
            else:
                pod = PodDocument(
                    order_id=order.id,
                    tracking_number=delivery,
                    file_name=pdf_f.name,
                    file_path=str(pdf_f),
                    source='folder_scan',
                )
                db.add(pod)
                await db.flush()

            # Update or create the registry entry
            if not reg:
                reg = PodRegistry(delivery_number=delivery)
                db.add(reg)
            reg.status = 'have_pod'
            reg.filename = pdf_f.name
            reg.pod_folder_path = str(pdf_f)
            reg.order_id = order.id
            reg.received_at = datetime.now()
            reg.received_via = 'manual'
            reg.matched_by = 'folder_scan'
            await db.flush()

            logger.info("POD folder scan: matched %s → %s", pdf_f.name, delivery)
            return pod

    return None


def _reg_out(e: PodRegistry) -> dict:
    return {
        'id': str(e.id),
        'delivery_number': e.delivery_number,
        'customer_po': e.customer_po,
        'order_id': str(e.order_id) if e.order_id else None,
        'carrier_id': str(e.carrier_id) if e.carrier_id else None,
        'status': str(e.status),
        'filename': e.filename,
        'pod_folder_path': e.pod_folder_path,
        'requested_at': e.requested_at.isoformat() if e.requested_at else None,
        'received_at': e.received_at.isoformat() if e.received_at else None,
        'received_via': str(e.received_via) if e.received_via else None,
        'matched_by': e.matched_by,
        'notes': e.notes,
        'is_deleted': bool(e.is_deleted),
        'deleted_at': e.deleted_at.isoformat() if e.deleted_at else None,
        'created_at': e.created_at.isoformat() if e.created_at else None,
    }
