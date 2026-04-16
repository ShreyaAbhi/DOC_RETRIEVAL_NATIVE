"""
document_prereader_service.py

Pre-processing step that runs BEFORE the folder scan.
For each file in pod_storage / packing_slips / invoices:
  1. Extract text from the document (pypdf for text-based PDFs;
     LibreOffice→text for office docs; images skipped — no OCR available).
  2. Search that text for any known reference number from the orders table
     (my_delivery_number, invoice_number, customer_order_number).
  3. If a match is found and the reference is not already in the filename,
     prepend it: "0080202148_original_name.pdf"

After renaming, the existing scan_order_documents_task can find the files
by the normal delivery-number / invoice-number substring match.
"""
import logging
import asyncio
from pathlib import Path
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.config import settings
from app.services.pdf_conversion_service import SCANNABLE_EXTENSIONS, IMAGE_EXTENSIONS, OFFICE_EXTENSIONS

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------

def _extract_pdf_text(path: Path) -> str:
    """Extract text layer from a PDF using pypdf. Returns '' for image-only PDFs."""
    try:
        import pypdf
        reader = pypdf.PdfReader(str(path))
        pages = []
        for page in reader.pages:
            t = page.extract_text()
            if t:
                pages.append(t)
        return "\n".join(pages)
    except Exception as e:
        logger.debug("pypdf could not extract text from %s: %s", path.name, e)
        return ""


def _ocr_image_file(path: Path) -> str:
    """Run Tesseract OCR on an image file."""
    try:
        import sys
        import pytesseract
        from PIL import Image
        if sys.platform == 'win32':
            import os
            tess_cmd = os.environ.get(
                'TESSERACT_CMD',
                r'C:\Program Files\Tesseract-OCR\tesseract.exe'
            )
            pytesseract.pytesseract.tesseract_cmd = tess_cmd
        img = Image.open(str(path))
        return pytesseract.image_to_string(img)
    except Exception as e:
        logger.debug("Tesseract OCR failed for %s: %s", path.name, e)
        return ""


def _ocr_pdf(path: Path) -> str:
    """Convert each PDF page to an image and OCR it (for scanned/image-based PDFs)."""
    try:
        import sys
        from pdf2image import convert_from_path
        import pytesseract
        if sys.platform == 'win32':
            import os
            tess_cmd = os.environ.get(
                'TESSERACT_CMD',
                r'C:\Program Files\Tesseract-OCR\tesseract.exe'
            )
            pytesseract.pytesseract.tesseract_cmd = tess_cmd
        pages = convert_from_path(str(path), dpi=200)
        texts = []
        for img in pages:
            t = pytesseract.image_to_string(img)
            if t.strip():
                texts.append(t)
        return "\n".join(texts)
    except Exception as e:
        logger.debug("PDF OCR failed for %s: %s", path.name, e)
        return ""


async def _extract_office_text(path: Path) -> str:
    """Convert office doc to PDF via LibreOffice, then extract text."""
    import tempfile, shutil
    from app.services.pdf_conversion_service import convert_to_pdf
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_src = Path(tmpdir) / path.name
            shutil.copy2(str(path), str(tmp_src))
            pdf_path = await convert_to_pdf(str(tmp_src))
            if pdf_path:
                return await extract_text(pdf_path)
    except Exception as e:
        logger.debug("Office text extraction failed for %s: %s", path.name, e)
    return ""


async def extract_text(file_path: str) -> str:
    """
    Extract readable text from a file.
    PDF (text-based)  → pypdf
    PDF (image-based) → pdf2image + Tesseract OCR
    Images            → Tesseract OCR
    Office docs       → LibreOffice → PDF → (above)
    """
    p = Path(file_path)
    ext = p.suffix.lower()

    if ext == ".pdf":
        text = _extract_pdf_text(p)
        if text.strip():
            return text
        # No text layer — try OCR (scanned PDF)
        logger.debug("extract_text: no text layer in %s, trying OCR", p.name)
        return await asyncio.to_thread(_ocr_pdf, p)

    if ext in IMAGE_EXTENSIONS:
        return await asyncio.to_thread(_ocr_image_file, p)

    if ext in OFFICE_EXTENSIONS:
        return await _extract_office_text(p)

    return ""


# ---------------------------------------------------------------------------
# Reference matching
# ---------------------------------------------------------------------------

def _norm(s: str) -> str:
    """Normalise a reference string for comparison: strip separators, lowercase."""
    return s.replace("-", "").replace("_", "").replace(" ", "").lower()


def _find_reference_in_text(text: str, refs: list[tuple[str, str]]) -> Optional[tuple[str, str]]:
    """
    Search normalised text for any reference value.
    refs: list of (ref_value, ref_type) e.g. ("0080202148", "delivery")
    Returns the first (ref_value, ref_type) found, or None.
    """
    text_norm = _norm(text)
    for ref_value, ref_type in refs:
        if not ref_value:
            continue
        if _norm(ref_value) in text_norm:
            return ref_value, ref_type
    return None


# ---------------------------------------------------------------------------
# Single-file processing
# ---------------------------------------------------------------------------

async def preread_file(file_path: str, refs: list[tuple[str, str]]) -> Optional[str]:
    """
    Extract text from file_path, search for a known reference, and rename
    the file by prepending the reference if found and not already present.

    Returns the new filename (stem only) if renamed, else None.
    """
    p = Path(file_path)
    fname_norm = _norm(p.stem)

    # Skip if any reference is already embedded in the filename
    for ref_value, _ in refs:
        if ref_value and _norm(ref_value) in fname_norm:
            logger.debug("preread: %s already contains a reference — skipping", p.name)
            return None

    text = await extract_text(file_path)
    if not text.strip():
        logger.debug("preread: no text extracted from %s", p.name)
        return None

    match = _find_reference_in_text(text, refs)
    if not match:
        logger.debug("preread: no reference found in %s", p.name)
        return None

    ref_value, ref_type = match
    new_name = f"{ref_value}_{p.name}"
    new_path = p.parent / new_name

    # Avoid overwriting an existing file
    if new_path.exists():
        logger.warning("preread: target %s already exists — skipping rename of %s", new_name, p.name)
        return None

    p.rename(new_path)
    logger.info("preread: renamed %s → %s (matched %s: %s)", p.name, new_name, ref_type, ref_value)
    return new_name


# ---------------------------------------------------------------------------
# Folder scan
# ---------------------------------------------------------------------------

async def preread_folder(folder_path: str, refs: list[tuple[str, str]]) -> dict:
    """
    Run preread_file on every unprocessed document in a folder.
    Returns {"renamed": N, "no_text": N, "no_match": N, "errors": N, "skipped": N}
    """
    folder = Path(folder_path)
    if not folder.exists():
        logger.warning("preread_folder: folder does not exist: %s", folder_path)
        return {"renamed": 0, "no_text": 0, "no_match": 0, "errors": 0, "skipped": 0}

    counts = {"renamed": 0, "no_text": 0, "no_match": 0, "errors": 0, "skipped": 0}

    for f in sorted(folder.iterdir()):
        if not f.is_file():
            continue
        if f.suffix.lower() not in SCANNABLE_EXTENSIONS:
            continue
        # Skip files in processed/ subdirectory
        if f.parent.name == "processed":
            continue

        # Check if already prefixed with a known reference
        fname_norm = _norm(f.stem)
        already_prefixed = any(ref_value and _norm(ref_value) in fname_norm for ref_value, _ in refs)
        if already_prefixed:
            counts["skipped"] += 1
            continue

        try:
            result = await preread_file(str(f), refs)
            if result is None:
                text = await extract_text(str(f))
                if not text.strip():
                    counts["no_text"] += 1
                else:
                    counts["no_match"] += 1
            else:
                counts["renamed"] += 1
        except Exception as e:
            logger.error("preread_folder: error processing %s: %s", f.name, e)
            counts["errors"] += 1

    return counts


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def preread_all_folders(db: AsyncSession) -> dict:
    """
    Load all reference numbers from the orders table, then run preread on
    pod_storage, packing_slips, and invoices folders.
    """
    from app.models.models import Order
    from app.db.session import AsyncSessionLocal

    result = await db.execute(
        select(Order.my_delivery_number, Order.invoice_number, Order.customer_order_number)
    )
    rows = result.all()

    # Build reference list: (value, type) — delivery first for priority
    refs: list[tuple[str, str]] = []
    for dn, inv, cpo in rows:
        if dn:
            refs.append((str(dn), "delivery"))
        if inv:
            refs.append((str(inv), "invoice"))
        if cpo:
            refs.append((str(cpo), "customer_po"))

    if not refs:
        return {"pod_storage": {}, "packing_slips": {}, "invoices": {}, "total_refs": 0}

    # Get folder paths from system_config (fall back to settings)
    from app.models.models import SystemConfig
    pod_cfg  = await db.get(SystemConfig, "pod_folder_path")
    slip_cfg = await db.get(SystemConfig, "packing_slip_folder_path")
    inv_cfg  = await db.get(SystemConfig, "invoice_folder_path")

    pod_folder  = (pod_cfg.value  if pod_cfg  else None) or settings.POD_STORAGE_PATH
    slip_folder = (slip_cfg.value if slip_cfg else None) or settings.PACKING_SLIPS_PATH
    inv_folder  = (inv_cfg.value  if inv_cfg  else None) or settings.INVOICES_PATH

    pod_result  = await preread_folder(pod_folder,  refs)
    slip_result = await preread_folder(slip_folder, refs)
    inv_result  = await preread_folder(inv_folder,  refs)

    total_renamed = pod_result["renamed"] + slip_result["renamed"] + inv_result["renamed"]
    logger.info(
        "preread_all_folders complete: %d renamed (pod=%d slip=%d inv=%d)",
        total_renamed, pod_result["renamed"], slip_result["renamed"], inv_result["renamed"],
    )

    return {
        "total_refs":   len(refs),
        "pod_storage":  pod_result,
        "packing_slips": slip_result,
        "invoices":     inv_result,
        "total_renamed": total_renamed,
    }
