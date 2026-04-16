"""
pdf_conversion_service.py
Converts non-PDF documents to PDF before they are stored in the database.

Supported formats:
  Images  : .png .jpg .jpeg .tiff .tif .bmp .gif  → Pillow (already installed)
  Office  : .docx .doc .odt .rtf .xlsx .xls .ods  → LibreOffice headless

Usage:
    pdf_path = await convert_to_pdf("/app/packing_slips/slip.jpg")
    # Returns "/app/packing_slips/slip.pdf", or None on failure.

Guards:
  - If the file is already a PDF, returns the source path unchanged.
  - If a converted PDF already exists next to the source, returns that path
    without re-converting (idempotent).
  - All failures are logged and return None so callers skip the file gracefully.
"""
import asyncio
import logging
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

IMAGE_EXTENSIONS  = {'.png', '.jpg', '.jpeg', '.tiff', '.tif', '.bmp', '.gif'}
OFFICE_EXTENSIONS = {'.docx', '.doc', '.odt', '.rtf', '.xlsx', '.xls', '.ods'}
CONVERTIBLE_EXTENSIONS = IMAGE_EXTENSIONS | OFFICE_EXTENSIONS

# All extensions the folder scanners should look for (PDF + everything we convert)
SCANNABLE_EXTENSIONS = CONVERTIBLE_EXTENSIONS | {'.pdf'}


async def convert_bytes_to_pdf(file_bytes: bytes, filename: str) -> tuple:
    """
    Convert raw file bytes to PDF bytes.

    Returns (pdf_bytes, pdf_filename) where pdf_filename has a .pdf extension.
    If the file is already a PDF, returns the original bytes and filename unchanged.
    Raises ValueError if the format is unsupported or conversion fails.
    """
    import tempfile

    p = Path(filename)
    ext = p.suffix.lower()
    pdf_filename = p.stem + '.pdf'

    if ext == '.pdf':
        return file_bytes, filename

    if ext not in CONVERTIBLE_EXTENSIONS:
        raise ValueError(
            f"Unsupported file format '{ext}'. "
            f"Accepted: .pdf, images (.jpg .png .tiff .bmp .gif), "
            f"office docs (.docx .xlsx .odt .ods .rtf .doc .xls)"
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        src = Path(tmpdir) / filename
        src.write_bytes(file_bytes)
        pdf_path_str = await convert_to_pdf(str(src))
        if not pdf_path_str:
            raise ValueError(f"Conversion to PDF failed for '{filename}'")
        pdf_bytes = Path(pdf_path_str).read_bytes()

    return pdf_bytes, pdf_filename


async def convert_to_pdf(source_path: str) -> Optional[str]:
    """
    Convert *source_path* to PDF if it is not already one.
    Returns the path to the PDF file, or None if conversion is not possible
    or fails.
    """
    p = Path(source_path)
    ext = p.suffix.lower()

    if ext == '.pdf':
        return source_path

    if not p.exists():
        logger.warning("pdf_conversion: source file not found: %s", source_path)
        return None

    # Reuse an existing converted PDF if already present
    pdf_path = p.with_suffix('.pdf')
    if pdf_path.exists():
        logger.debug("pdf_conversion: reusing existing %s", pdf_path)
        return str(pdf_path)

    if ext in IMAGE_EXTENSIONS:
        return _convert_image(p, pdf_path)

    if ext in OFFICE_EXTENSIONS:
        # LibreOffice is synchronous/CPU-bound — run in a thread pool
        return await asyncio.to_thread(_convert_office, p, pdf_path)

    logger.warning("pdf_conversion: unsupported extension %s for %s", ext, source_path)
    return None


def _convert_image(source: Path, dest: Path) -> Optional[str]:
    """Convert an image file to a single-page PDF using Pillow."""
    try:
        from PIL import Image
        img = Image.open(source)
        # Ensure RGB — PDFs cannot contain palette or RGBA modes directly
        if img.mode not in ('RGB', 'L'):
            img = img.convert('RGB')
        # Multi-frame images (animated GIF, multi-page TIFF) → first frame only
        img.save(str(dest), 'PDF', resolution=150)
        logger.info("pdf_conversion: image → pdf  %s → %s", source.name, dest.name)
        return str(dest)
    except Exception as e:
        logger.error("pdf_conversion: image conversion failed for %s: %s", source, e)
        return None


def _convert_office(source: Path, dest: Path) -> Optional[str]:
    """Convert an office document to PDF using LibreOffice headless."""
    import sys
    # On Windows the executable is 'soffice'; on Linux/Mac it's 'libreoffice'
    lo_cmd = 'soffice' if sys.platform == 'win32' else 'libreoffice'
    try:
        result = subprocess.run(
            [
                lo_cmd, '--headless',
                '--convert-to', 'pdf',
                '--outdir', str(source.parent),
                str(source),
            ],
            timeout=60,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.error(
                "pdf_conversion: libreoffice failed for %s (rc=%d): %s",
                source, result.returncode, result.stderr,
            )
            return None
        if not dest.exists():
            logger.error("pdf_conversion: libreoffice ran but output not found: %s", dest)
            return None
        logger.info("pdf_conversion: office → pdf  %s → %s", source.name, dest.name)
        return str(dest)
    except FileNotFoundError:
        logger.error("pdf_conversion: libreoffice not installed — cannot convert %s", source)
        return None
    except subprocess.TimeoutExpired:
        logger.error("pdf_conversion: libreoffice timed out for %s", source)
        return None
    except Exception as e:
        logger.error("pdf_conversion: office conversion failed for %s: %s", source, e)
        return None
