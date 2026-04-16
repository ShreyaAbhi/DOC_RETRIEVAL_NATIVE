from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_
from pydantic import BaseModel
from typing import Optional

from app.db.session import get_db
from app.models.models import MaterialMaster, User
from app.core.security import require_admin, get_current_user

router = APIRouter()


class MaterialIn(BaseModel):
    material_number: str
    description: str
    unit_of_measure: Optional[str] = "EA"


@router.get("")
async def list_materials(search: Optional[str] = None, limit: int = 200, offset: int = 0, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    stmt = select(MaterialMaster).where(MaterialMaster.is_active == True)
    if search:
        term = f"%{search}%"
        stmt = stmt.where(or_(
            MaterialMaster.material_number.ilike(term),
            MaterialMaster.description.ilike(term),
        ))
        limit = 1000
    result = await db.execute(stmt.order_by(MaterialMaster.material_number).limit(limit).offset(offset))
    return [_out(m) for m in result.scalars().all()]


@router.post("")
async def create_material(data: MaterialIn, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    m = MaterialMaster(**data.model_dump())
    db.add(m)
    await db.commit()
    return _out(m)


@router.put("/{material_id}")
async def update_material(material_id: str, data: MaterialIn, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    m = await db.get(MaterialMaster, material_id)
    if not m:
        raise HTTPException(404)
    m.material_number = data.material_number
    m.description = data.description
    m.unit_of_measure = data.unit_of_measure
    await db.commit()
    return _out(m)


@router.delete("/{material_id}")
async def delete_material(material_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    m = await db.get(MaterialMaster, material_id)
    if not m:
        raise HTTPException(404)
    m.is_active = False
    await db.commit()
    return {"deleted": True}


def _out(m: MaterialMaster):
    return {
        "id": str(m.id),
        "material_number": m.material_number,
        "description": m.description,
        "unit_of_measure": m.unit_of_measure,
        "is_active": m.is_active,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }
