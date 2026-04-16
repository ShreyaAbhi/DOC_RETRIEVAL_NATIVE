"""
v1_documents.py
External API for agents/users to query POD, packing slip, and invoice documents.
Authentication: X-API-Key header (API key, not JWT).
"""
import os
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Form
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.session import get_db
from app.core.security import get_api_key
from app.core.config import settings
from app.models.models import (
    PodRegistry, Order, OrderLine, PackingSlipDocument, InvoiceDocument, Carrier
)
from app.services.pod_folder_service import get_pod_folder, mark_pod_requested, save_pod_bytes

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _resolve_registry(
    db: AsyncSession,
    delivery_number: Optional[str],
    customer_po: Optional[str],
    order_number: Optional[str],
) -> Optional[PodRegistry]:
    """Find a PodRegistry entry from any of the three lookup keys."""
    if delivery_number:
        r = await db.execute(
            select(PodRegistry).where(PodRegistry.delivery_number == delivery_number)
        )
        entry = r.scalar_one_or_none()
        if entry:
            return entry

    if customer_po:
        r = await db.execute(
            select(PodRegistry).where(PodRegistry.customer_po == customer_po)
        )
        entry = r.scalars().first()
        if entry:
            return entry

    if order_number:
        # Resolve order by customer_order_number
        r = await db.execute(
            select(Order).where(Order.customer_order_number == order_number)
        )
        order = r.scalar_one_or_none()
        if order:
            r2 = await db.execute(
                select(PodRegistry).where(PodRegistry.order_id == order.id)
            )
            entry = r2.scalars().first()
            if entry:
                return entry

    return None


async def _get_packing_slip(db: AsyncSession, entry: PodRegistry) -> Optional[PackingSlipDocument]:
    if entry.delivery_number:
        r = await db.execute(
            select(PackingSlipDocument).where(
                PackingSlipDocument.delivery_number == entry.delivery_number
            )
        )
        slip = r.scalar_one_or_none()
        if slip:
            return slip
    if entry.order_id:
        r = await db.execute(
            select(PackingSlipDocument).where(
                PackingSlipDocument.order_id == entry.order_id
            )
        )
        return r.scalars().first()
    return None


async def _get_invoice(db: AsyncSession, entry: PodRegistry) -> Optional[InvoiceDocument]:
    if entry.order_id:
        r = await db.execute(
            select(InvoiceDocument).where(InvoiceDocument.order_id == entry.order_id)
        )
        return r.scalars().first()
    return None


def _doc_info(label: str, available: bool, filename: Optional[str]) -> dict:
    return {
        "available": available,
        "file_name": filename,
        "download_url": f"/api/v1/documents/download/{label}/{filename}" if available and filename else None,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/lookup")
async def lookup_documents(
    delivery_number: Optional[str] = Query(None, description="Carrier delivery / tracking number"),
    customer_po: Optional[str] = Query(None, description="Customer purchase order number"),
    order_number: Optional[str] = Query(None, description="Customer order number"),
    request_if_missing: bool = Query(False, description="Send carrier email if POD is missing"),
    db: AsyncSession = Depends(get_db),
    _key=Depends(get_api_key),
):
    """
    Look up all available documents for a shipment.

    Supply at least one of: delivery_number, customer_po, order_number.
    Returns POD, packing slip, and invoice status plus download URLs.
    If request_if_missing=true and the POD is not available, a request email
    is sent to the carrier (if configured).
    """
    if not delivery_number and not customer_po and not order_number:
        raise HTTPException(
            status_code=400,
            detail="Provide at least one of: delivery_number, customer_po, order_number",
        )

    entry = await _resolve_registry(db, delivery_number, customer_po, order_number)

    pod_available = entry is not None and str(entry.status) == "have_pod" and bool(entry.filename)
    slip = await _get_packing_slip(db, entry) if entry else None
    inv = await _get_invoice(db, entry) if entry else None

    # Optionally trigger POD request
    requested_now = False
    if not pod_available and request_if_missing and entry and entry.delivery_number:
        if str(entry.status) not in ("requested", "have_pod"):
            carrier_id = str(entry.carrier_id) if entry.carrier_id else None
            order_id   = str(entry.order_id)   if entry.order_id   else None
            await mark_pod_requested(db, entry.delivery_number, carrier_id=carrier_id, order_id=order_id)
            requested_now = True

    return {
        "delivery_number": entry.delivery_number if entry else delivery_number,
        "customer_po":     entry.customer_po     if entry else customer_po,
        "order_id":        str(entry.order_id)   if entry and entry.order_id else None,
        "pod": {
            **_doc_info("pod", pod_available, entry.filename if entry else None),
            "status": str(entry.status) if entry else "not_found",
            "received_at": entry.received_at.isoformat() if entry and entry.received_at else None,
            "received_via": str(entry.received_via) if entry and entry.received_via else None,
            "requested_now": requested_now,
        },
        "packing_slip": _doc_info("packing-slip", slip is not None, slip.file_name if slip else None),
        "invoice":      _doc_info("invoice",       inv  is not None, inv.file_name  if inv  else None),
    }


@router.post("/upload/pod")
async def upload_pod(
    file: UploadFile = File(..., description="POD PDF file"),
    delivery_number: Optional[str] = Form(None, description="Carrier delivery / tracking number"),
    customer_po: Optional[str] = Form(None, description="Customer PO number"),
    order_number: Optional[str] = Form(None, description="Customer order number"),
    db: AsyncSession = Depends(get_db),
    _key=Depends(get_api_key),
):
    """
    Upload a POD document via the external API.

    The registry entry matching delivery_number / customer_po / order_number is
    updated to status='have_pod' and the file is saved to POD storage.
    Supply at least one identifier.
    """
    if not delivery_number and not customer_po and not order_number:
        raise HTTPException(
            status_code=400,
            detail="Provide at least one of: delivery_number, customer_po, order_number",
        )

    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in (".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".tif"):
        raise HTTPException(status_code=400, detail="Unsupported file type. Use PDF or image.")

    # Read file bytes once; validate size and magic numbers before saving
    file_bytes = await file.read()
    MAX_SIZE = 25 * 1024 * 1024  # 25 MB
    if len(file_bytes) > MAX_SIZE:
        raise HTTPException(status_code=413, detail="File too large. Maximum size is 25 MB.")

    _MAGIC = {
        b"%PDF":         ".pdf",
        b"\x89PNG\r\n":  ".png",
        b"\xff\xd8\xff": ".jpg",
        b"II*\x00":      ".tiff",   # TIFF little-endian
        b"MM\x00*":      ".tiff",   # TIFF big-endian
    }
    detected = None
    for magic, mime_ext in _MAGIC.items():
        if file_bytes[:len(magic)] == magic:
            detected = mime_ext
            break
    if detected is None:
        raise HTTPException(status_code=400, detail="File content does not match a supported type (PDF, PNG, JPEG, TIFF).")
    # Normalise: treat .jpeg/.tif as .jpg/.tiff for magic check
    normalised_ext = ".jpg" if ext == ".jpeg" else (".tiff" if ext == ".tif" else ext)
    if detected != normalised_ext and not (detected == ".jpg" and normalised_ext in (".jpg",)):
        raise HTTPException(status_code=400, detail=f"File extension '{ext}' does not match file content.")

    # Resolve existing registry entry to pick up linked IDs
    entry = await _resolve_registry(db, delivery_number, customer_po, order_number)

    # Use delivery_number from the registry if not supplied directly
    resolved_dn = delivery_number or (entry.delivery_number if entry else None)
    if not resolved_dn:
        raise HTTPException(
            status_code=422,
            detail="Could not determine a delivery number. Pass delivery_number explicitly.",
        )

    resolved_order_id  = str(entry.order_id)  if entry and entry.order_id  else None
    resolved_carrier_id = str(entry.carrier_id) if entry and entry.carrier_id else None
    resolved_po        = customer_po or (entry.customer_po if entry else None)

    if not file_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    saved_filename = await save_pod_bytes(
        db=db,
        file_bytes=file_bytes,
        delivery_number=resolved_dn,
        original_filename=file.filename,
        received_via="manual",
        matched_by="api",
        carrier_id=resolved_carrier_id,
        order_id=resolved_order_id,
        customer_po=resolved_po,
    )

    if not saved_filename:
        raise HTTPException(status_code=500, detail="Failed to save POD file")

    logger.info("POD uploaded via API: %s → %s", resolved_dn, saved_filename)

    return {
        "status": "have_pod",
        "delivery_number": resolved_dn,
        "customer_po": resolved_po,
        "file_name": saved_filename,
        "download_url": f"/api/v1/documents/download/pod/{saved_filename}",
    }


@router.get("/download/pod/{filename}")
async def download_pod(
    filename: str,
    db: AsyncSession = Depends(get_db),
    _key=Depends(get_api_key),
):
    """Download a POD PDF file."""
    # Prevent path traversal
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    pod_folder = await get_pod_folder(db)
    path = os.path.join(pod_folder, filename)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="POD file not found")
    return FileResponse(path, media_type="application/pdf", filename=filename)


@router.get("/download/packing-slip/{filename}")
async def download_packing_slip(
    filename: str,
    _key=Depends(get_api_key),
):
    """Download a packing slip file."""
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    path = os.path.join(settings.PACKING_SLIPS_PATH, filename)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Packing slip file not found")
    return FileResponse(path, media_type="application/pdf", filename=filename)


@router.get("/download/invoice/{filename}")
async def download_invoice(
    filename: str,
    _key=Depends(get_api_key),
):
    """Download an invoice file."""
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    path = os.path.join(settings.INVOICES_PATH, filename)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Invoice file not found")
    return FileResponse(path, media_type="application/pdf", filename=filename)
