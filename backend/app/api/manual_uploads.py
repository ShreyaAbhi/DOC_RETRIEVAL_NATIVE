"""
manual_uploads.py
Endpoints for manually uploading POD, packing slip, and invoice documents
scoped to a specific EmailRequest (and the delivery numbers the LLM identified).
"""
import re
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.session import get_db
from app.models.models import (
    EmailRequest, Order, PodRegistry, PackingSlipDocument, InvoiceDocument, User
)
from app.core.security import require_reviewer_or_admin
from app.services.document_upload_service import upload_pod, upload_packing_slip, upload_invoice

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Order ID extraction (mirrors pipeline._extract_all_order_ids) ──

def _extract_order_ids_from_req(req: EmailRequest) -> list[str]:
    """
    Return the deduplicated list of order IDs the LLM found in this email.
    Replicates pipeline._extract_all_order_ids exactly:
      1. classification_raw.orderIds (Ollama output)
      2. Regex scan of subject + body for ORD-NNNN patterns
      3. Fallback to extracted_order_id
    """
    cls = req.classification_raw or {}

    ollama_ids = cls.get("orderIds") or []
    if isinstance(ollama_ids, str):
        ollama_ids = [ollama_ids]
    ollama_ids = [o.upper() for o in ollama_ids if o]

    text = (req.subject or "") + " " + (req.body or "")
    regex_ids = [m.upper() for m in re.findall(r'ORD-\d+', text, re.I)]

    seen: set = set()
    result: list = []
    for oid in (ollama_ids + regex_ids):
        if oid not in seen:
            seen.add(oid)
            result.append(oid)

    if not result and req.extracted_order_id:
        result = [req.extracted_order_id]

    return result


async def _get_all_dns_with_orders(
    db: AsyncSession, req: EmailRequest
) -> list[tuple[str, Optional[Order]]]:
    """
    Return (delivery_number, order_or_None) for EVERY identifier the LLM extracted.

    - If the identifier resolves to an Order (by customer_order_number or
      my_delivery_number), the order's my_delivery_number is used as the key.
    - If no Order is found, the raw extracted string is used as the delivery
      number with order=None so the row still appears in the modal.
    - Deduplicates by both delivery_number and order_id to avoid double rows.
    """
    order_ids = _extract_order_ids_from_req(req)
    seen_dns: set = set()
    seen_order_ids: set = set()
    result: list = []

    for oid_str in order_ids:
        # Primary: match by customer_order_number
        r = await db.execute(
            select(Order).where(Order.customer_order_number == oid_str)
        )
        order = r.scalar_one_or_none()

        # Fallback: match by my_delivery_number
        if not order:
            r = await db.execute(
                select(Order).where(Order.my_delivery_number == oid_str)
            )
            order = r.scalar_one_or_none()

        if order:
            oid = str(order.id)
            if oid not in seen_order_ids:
                seen_order_ids.add(oid)
                dn = order.my_delivery_number or oid_str
                if dn not in seen_dns:
                    seen_dns.add(dn)
                    result.append((dn, order))
        else:
            # No matching order — keep the raw string as the delivery number
            if oid_str not in seen_dns:
                seen_dns.add(oid_str)
                result.append((oid_str, None))

    return result


async def _valid_delivery_numbers(db: AsyncSession, req: EmailRequest) -> set[str]:
    """Return ALL delivery numbers valid for this request (resolved or not)."""
    pairs = await _get_all_dns_with_orders(db, req)
    return {dn for dn, _ in pairs if dn}


async def _resolve_order_for_delivery(
    db: AsyncSession, delivery_number: str
) -> Optional[Order]:
    r = await db.execute(
        select(Order).where(Order.my_delivery_number == delivery_number)
    )
    return r.scalar_one_or_none()


# ── GET /missing-docs ─────────────────────────────────────────

@router.get("/{request_id}/missing-docs")
async def get_missing_docs(
    request_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_reviewer_or_admin),
):
    """
    Return document status per delivery number identified by the LLM in this request.
    Shows which of POD / packing slip / invoice are present or missing for each order.
    """
    req = await db.get(EmailRequest, request_id)
    if not req:
        raise HTTPException(404, "Request not found")

    pairs = await _get_all_dns_with_orders(db, req)
    result = []

    for dn, order in pairs:
        order_id = order.id if order else None

        # ── POD status ──────────────────────────────────────────
        pod_status = "missing"
        pod_filename = None
        r = await db.execute(
            select(PodRegistry).where(PodRegistry.delivery_number == dn)
        )
        reg = r.scalar_one_or_none()
        if reg:
            pod_status = str(reg.status)
            pod_filename = reg.filename if str(reg.status) == 'have_pod' else None

        # ── Packing slip status ──────────────────────────────────
        slip_status = "missing"
        slip_filename = None
        r = await db.execute(
            select(PackingSlipDocument).where(PackingSlipDocument.delivery_number == dn)
        )
        slip = r.scalar_one_or_none()
        if not slip and order_id:
            r = await db.execute(
                select(PackingSlipDocument).where(PackingSlipDocument.order_id == order_id)
            )
            slip = r.scalars().first()
        if slip:
            slip_status = "have_slip"
            slip_filename = slip.file_name

        # ── Invoice status ───────────────────────────────────────
        inv_status = "missing"
        inv_filename = None
        if order_id:
            r = await db.execute(
                select(InvoiceDocument).where(InvoiceDocument.order_id == order_id)
            )
            inv = r.scalars().first()
            if inv:
                inv_status = "have_invoice"
                inv_filename = inv.file_name

        result.append({
            "order_id": str(order_id) if order_id else None,
            "customer_order_number": order.customer_order_number if order else None,
            "my_delivery_number": dn,
            "invoice_number": order.invoice_number if order else None,
            "unlinked": order is None,
            "warning": (
                "Delivery number not found in Orders table — documents will be saved "
                "without an order link. Add this order to the system to fully link it."
                if order is None else None
            ),
            "pod": {"status": pod_status, "filename": pod_filename},
            "packing_slip": {"status": slip_status, "filename": slip_filename},
            "invoice": {"status": inv_status, "filename": inv_filename},
        })

    return {
        "request_id": request_id,
        "reference_number": req.reference_number,
        "request_status": str(req.status),
        "orders": result,
    }


# ── POST /upload/pod ──────────────────────────────────────────

@router.post("/{request_id}/upload/pod")
async def upload_pod_for_request(
    request_id: str,
    delivery_number: str = Form(...),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_reviewer_or_admin),
):
    """
    Manually upload a POD PDF for a delivery number that the LLM identified
    in this request.  Only delivery numbers linked to this request are accepted.
    """
    req = await db.get(EmailRequest, request_id)
    if not req:
        raise HTTPException(404, "Request not found")

    valid_dns = await _valid_delivery_numbers(db, req)
    if delivery_number not in valid_dns:
        raise HTTPException(
            400,
            f"Delivery number '{delivery_number}' is not associated with "
            f"request {req.reference_number}",
        )

    order = await _resolve_order_for_delivery(db, delivery_number)

    file_bytes = await file.read()
    try:
        saved = await upload_pod(
            db=db,
            delivery_number=delivery_number,
            file_bytes=file_bytes,
            original_filename=file.filename or "upload.pdf",
            order_id=str(order.id) if order else None,
            uploader_email=current_user.email,
            request_id=request_id,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    return {"saved": True, "filename": saved, "delivery_number": delivery_number}


# ── POST /upload/packing-slip ─────────────────────────────────

@router.post("/{request_id}/upload/packing-slip")
async def upload_slip_for_request(
    request_id: str,
    delivery_number: str = Form(...),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_reviewer_or_admin),
):
    """
    Manually upload a packing slip PDF for a delivery number that the LLM
    identified in this request.
    """
    req = await db.get(EmailRequest, request_id)
    if not req:
        raise HTTPException(404, "Request not found")

    valid_dns = await _valid_delivery_numbers(db, req)
    if delivery_number not in valid_dns:
        raise HTTPException(
            400,
            f"Delivery number '{delivery_number}' is not associated with "
            f"request {req.reference_number}",
        )

    order = await _resolve_order_for_delivery(db, delivery_number)

    file_bytes = await file.read()
    try:
        saved = await upload_packing_slip(
            db=db,
            delivery_number=delivery_number,
            order_id=str(order.id) if order else None,
            file_bytes=file_bytes,
            original_filename=file.filename or "upload.pdf",
            uploader_email=current_user.email,
            request_id=request_id,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    return {"saved": True, "filename": saved, "delivery_number": delivery_number}


# ── POST /upload/invoice ──────────────────────────────────────

@router.post("/{request_id}/upload/invoice")
async def upload_invoice_for_request(
    request_id: str,
    delivery_number: str = Form(...),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_reviewer_or_admin),
):
    """
    Manually upload an invoice PDF for a delivery number that the LLM
    identified in this request.
    """
    req = await db.get(EmailRequest, request_id)
    if not req:
        raise HTTPException(404, "Request not found")

    valid_dns = await _valid_delivery_numbers(db, req)
    if delivery_number not in valid_dns:
        raise HTTPException(
            400,
            f"Delivery number '{delivery_number}' is not associated with "
            f"request {req.reference_number}",
        )

    order = await _resolve_order_for_delivery(db, delivery_number)

    file_bytes = await file.read()
    try:
        saved = await upload_invoice(
            db=db,
            delivery_number=delivery_number,
            order_id=str(order.id) if order else None,
            invoice_number=order.invoice_number if order else None,
            file_bytes=file_bytes,
            original_filename=file.filename or "upload.pdf",
            uploader_email=current_user.email,
            request_id=request_id,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    return {"saved": True, "filename": saved, "delivery_number": delivery_number}
