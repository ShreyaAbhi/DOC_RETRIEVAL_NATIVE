from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from datetime import datetime
import os
from pathlib import Path

from app.db.session import get_db
from app.models.models import SystemConfig, User
from app.core.config import settings
from app.services.audit_service import log_audit

LOGO_DIR = Path(settings.POD_STORAGE_PATH) / "branding"
LOGO_PATH = LOGO_DIR / "logo"  # extension appended on save
ALLOWED_IMAGE_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp", "image/svg+xml": ".svg"}
from app.core.security import require_admin

router = APIRouter()

LLM_PROMPT_KEYS = {
    "llm_classify_system_prompt",
    "llm_classify_user_preamble",
    "llm_response_system_prompt",
    "llm_response_instructions",
}
MASKED_CONFIG_KEYS = {
    "smtp_password", "ftp_password",
    "ups_client_secret", "fedex_client_secret", "dhl_api_key", "purolator_api_key",
    "llm_openai_api_key", "llm_anthropic_api_key",
    "microsoft_oauth_client_secret",
}
_MASK = "••••••••"


class ConfigUpdate(BaseModel):
    value: str


@router.get("")
async def get_config(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    result = await db.execute(select(SystemConfig))
    out = {}
    for r in result.scalars().all():
        val = r.value
        if r.key in MASKED_CONFIG_KEYS and val:
            val = _MASK
        out[r.key] = {"value": val, "description": r.description}
    return out


@router.put("/{key}")
async def update_config(key: str, body: ConfigUpdate, db: AsyncSession = Depends(get_db), current_user: User = Depends(require_admin)):
    if key in LLM_PROMPT_KEYS and str(current_user.role) != "super_admin":
        raise HTTPException(403, "Only the super admin can modify LLM prompt configuration")
    # Ignore saves where the admin didn't change a masked field (value is still the mask)
    if key in MASKED_CONFIG_KEYS and body.value.replace("•", "").strip() == "":
        cfg = await db.get(SystemConfig, key)
        return {"key": key, "value": _MASK if (cfg and cfg.value) else ""}
    cfg = await db.get(SystemConfig, key)
    old_value = cfg.value if cfg else None
    if not cfg:
        cfg = SystemConfig(key=key)
        db.add(cfg)
    cfg.value = body.value
    cfg.updated_at = datetime.utcnow()
    display_value = _MASK if key in MASKED_CONFIG_KEYS else body.value
    await log_audit(
        db, None, "system",
        f"Config '{key}' updated by {current_user.email}",
        {"key": key, "old": _MASK if key in MASKED_CONFIG_KEYS else old_value, "new": display_value},
        actor=current_user.email,
    )
    await db.commit()
    return {"key": key, "value": _MASK if key in MASKED_CONFIG_KEYS else cfg.value}


@router.get("/browse")
async def browse_directory(path: str = Query(default=None), _: User = Depends(require_admin)):
    try:
        p = Path(path).resolve() if path else Path(settings.POD_STORAGE_PATH).resolve().parent
        if not p.exists():
            raise HTTPException(400, f"Path does not exist: {path}")
        if not p.is_dir():
            raise HTTPException(400, f"Not a directory: {path}")
        dirs = []
        try:
            for entry in sorted(p.iterdir()):
                if entry.is_dir() and not entry.name.startswith('.'):
                    dirs.append({
                        "name": entry.name,
                        "path": str(entry),
                        "writable": os.access(str(entry), os.W_OK),
                    })
        except PermissionError:
            raise HTTPException(403, f"Permission denied reading: {path}")
        return {
            "path": str(p),
            "parent": str(p.parent) if str(p.parent) != str(p) else None,
            "dirs": dirs,
            "writable": os.access(str(p), os.W_OK),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


class PathValidation(BaseModel):
    path: str


@router.post("/validate-path")
async def validate_path(body: PathValidation, _: User = Depends(require_admin)):
    path = body.path.strip()
    if not path:
        return {"valid": False, "error": "Path cannot be empty"}
    p = Path(path)
    if not p.exists():
        try:
            p.mkdir(parents=True, exist_ok=True)
            return {"valid": True, "created": True, "path": str(p),
                    "warning": "Directory did not exist and was created automatically"}
        except PermissionError:
            return {"valid": False, "error": "Directory does not exist and cannot be created — permission denied"}
        except Exception as e:
            return {"valid": False, "error": f"Cannot create directory: {e}"}
    if not p.is_dir():
        return {"valid": False, "error": "Path exists but is not a directory (it may be a file)"}
    if not os.access(str(p), os.R_OK):
        return {"valid": False, "error": "Directory exists but is not readable by the application"}
    if not os.access(str(p), os.W_OK):
        return {"valid": False, "error": "Directory exists but is not writable — the application cannot store files here"}
    return {"valid": True, "path": str(p)}


def _find_logo_file() -> Path | None:
    """Return the current logo file path, or None if no logo is set."""
    for ext in ALLOWED_IMAGE_TYPES.values():
        candidate = LOGO_DIR / f"logo{ext}"
        if candidate.exists():
            return candidate
    return None


@router.get("/branding")
async def get_branding(db: AsyncSession = Depends(get_db)):
    """Public endpoint — no auth required. Returns app_name, app_version and whether a logo exists."""
    name_cfg = await db.get(SystemConfig, "app_name")
    ver_cfg  = await db.get(SystemConfig, "app_version")
    app_name = name_cfg.value if name_cfg and name_cfg.value else "Document Retrieval System"
    app_version = ver_cfg.value if ver_cfg and ver_cfg.value else "1.0.0"
    has_logo = _find_logo_file() is not None
    return {"app_name": app_name, "app_version": app_version, "has_logo": has_logo}


@router.get("/logo")
async def get_logo():
    """Public endpoint — serves the uploaded logo file."""
    logo = _find_logo_file()
    if not logo:
        raise HTTPException(404, "No logo uploaded")
    media_type = next((k for k, v in ALLOWED_IMAGE_TYPES.items() if logo.suffix == v), "image/png")
    return FileResponse(str(logo), media_type=media_type, headers={"Cache-Control": "public, max-age=3600"})


@router.post("/logo")
async def upload_logo(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Upload a new logo. Replaces any existing logo."""
    if file.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(400, f"Unsupported image type: {file.content_type}. Allowed: PNG, JPG, GIF, WebP, SVG")

    content = await file.read()
    if len(content) > 2 * 1024 * 1024:
        raise HTTPException(400, "Logo file too large — maximum 2 MB")

    LOGO_DIR.mkdir(parents=True, exist_ok=True)

    # Remove any previous logo (different extension)
    for ext in ALLOWED_IMAGE_TYPES.values():
        old = LOGO_DIR / f"logo{ext}"
        if old.exists():
            old.unlink()

    ext = ALLOWED_IMAGE_TYPES[file.content_type]
    dest = LOGO_DIR / f"logo{ext}"
    dest.write_bytes(content)

    # Store flag in system_config so branding endpoint reflects it
    cfg = await db.get(SystemConfig, "app_logo")
    if not cfg:
        cfg = SystemConfig(key="app_logo", description="Custom logo filename")
        db.add(cfg)
    cfg.value = f"logo{ext}"
    cfg.updated_at = datetime.utcnow()
    await db.commit()

    return {"filename": f"logo{ext}"}


@router.delete("/logo")
async def delete_logo(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    """Remove the current logo, reverting to the default icon."""
    logo = _find_logo_file()
    if logo and logo.exists():
        logo.unlink()
    cfg = await db.get(SystemConfig, "app_logo")
    if cfg:
        cfg.value = ""
        cfg.updated_at = datetime.utcnow()
        await db.commit()
    return {"status": "removed"}
