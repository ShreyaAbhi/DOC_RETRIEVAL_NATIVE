from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.models.models import User
from app.core.security import hash_password
from app.core.config import settings


async def seed_admin_if_empty(db: AsyncSession):
    """Create the default admin account on first run if no users exist."""
    result = await db.execute(select(func.count()).select_from(User))
    count = result.scalar()
    if count == 0:
        seed_email = settings.ADMIN_SEED_EMAIL.lower()
        seed_role = "super_admin" if seed_email == settings.VENDOR_EMAIL.lower() else "admin"
        admin = User(
            email=seed_email,
            full_name="System Admin",
            hashed_password=hash_password(settings.ADMIN_SEED_PASSWORD),
            role=seed_role,
            created_by="system",
        )
        db.add(admin)
        await db.commit()
