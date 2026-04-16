from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from typing import Optional

from app.db.session import get_db
from app.models.models import Carrier, User
from app.core.security import require_admin, get_current_user

router = APIRouter()


class CarrierIn(BaseModel):
    name: str
    email: Optional[str] = None
    sends_proactively: bool = False
    uses_ftp: bool = False
    ftp_subfolder: Optional[str] = None
    notes: Optional[str] = None
    is_active: bool = True


@router.get("")
async def list_carriers(
    limit: int = 200,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    result = await db.execute(select(Carrier).order_by(Carrier.name).limit(limit).offset(offset))
    return [_out(c) for c in result.scalars().all()]


@router.post("")
async def create_carrier(
    data: CarrierIn,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    carrier = Carrier(**data.model_dump(), created_by=admin.email)
    db.add(carrier)
    await db.commit()
    return _out(carrier)


@router.put("/{carrier_id}")
async def update_carrier(
    carrier_id: str,
    data: CarrierIn,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    carrier = await db.get(Carrier, carrier_id)
    if not carrier:
        raise HTTPException(404)
    for k, v in data.model_dump().items():
        setattr(carrier, k, v)
    await db.commit()
    return _out(carrier)


@router.delete("/{carrier_id}")
async def delete_carrier(
    carrier_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    carrier = await db.get(Carrier, carrier_id)
    if not carrier:
        raise HTTPException(404)
    carrier.is_active = False
    await db.commit()
    return {"deactivated": True}


def _out(c: Carrier):
    return {
        "id": str(c.id),
        "name": c.name,
        "email": c.email,
        "sends_proactively": c.sends_proactively,
        "uses_ftp": c.uses_ftp,
        "ftp_subfolder": c.ftp_subfolder,
        "notes": c.notes,
        "is_active": c.is_active,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "created_by": c.created_by,
    }
