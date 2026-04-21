from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
import os
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import get_db
from app.models.models import SystemConfig

router = APIRouter()


async def _get_search_paths(db: AsyncSession) -> list[str]:
    """Build list of directories to search for documents.
    Includes both SystemConfig-configured paths and settings defaults."""
    paths = []
    # Read configurable paths from SystemConfig first
    for key in ("pod_folder_path", "packing_slip_folder_path", "invoice_folder_path"):
        cfg = await db.get(SystemConfig, key)
        if cfg and cfg.value and cfg.value.strip():
            paths.append(cfg.value.strip())
    # Then add settings defaults as fallback
    for p in (settings.POD_STORAGE_PATH, settings.DOCUMENTS_PATH,
              settings.PACKING_SLIPS_PATH, settings.INVOICES_PATH):
        if p not in paths:
            paths.append(p)
    return paths


@router.get("/{filename}")
async def get_document(filename: str, db: AsyncSession = Depends(get_db)):
    filename = os.path.basename(filename)  # prevent path traversal
    search_paths = await _get_search_paths(db)
    for folder in search_paths:
        path = os.path.join(folder, filename)
        if os.path.exists(path):
            return FileResponse(path, media_type="application/pdf", filename=filename)
    raise HTTPException(404, "Document not found")
