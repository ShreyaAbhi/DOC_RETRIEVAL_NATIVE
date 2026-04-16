from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
import os
from app.core.config import settings

router = APIRouter()

SEARCH_PATHS = [
    settings.DOCUMENTS_PATH,
    settings.PACKING_SLIPS_PATH,
    settings.INVOICES_PATH,
    settings.POD_STORAGE_PATH,
]


@router.get("/{filename}")
async def get_document(filename: str):
    filename = os.path.basename(filename)  # prevent path traversal
    for folder in SEARCH_PATHS:
        path = os.path.join(folder, filename)
        if os.path.exists(path):
            return FileResponse(path, media_type="application/pdf", filename=filename)
    raise HTTPException(404, "Document not found")
