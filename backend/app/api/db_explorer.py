from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text, select
from sqlalchemy.exc import IntegrityError
from pydantic import BaseModel
from typing import List
import re

from app.db.session import get_db
from app.models.models import User
from app.core.security import require_admin, verify_password
from app.services.audit_service import log_audit

router = APIRouter()

# Whitelisted tables — all models in models.py
ALLOWED_TABLES = {
    "users",
    "material_master",
    "orders",
    "order_lines",
    "pod_documents",
    "packing_slip_documents",
    "invoice_documents",
    "email_requests",
    "approval_queue",
    "guidance_queue",
    "audit_logs",
    "monitored_emails",
    "system_config",
    "carriers",
    "api_keys",
    "pod_registry",
}

# Primary key column per table (hardcoded — not inferred from DB)
TABLE_PK = {
    "system_config": "key",   # PK is a string "key" column
    # all others use "id"
}

# Columns that must never be returned
REDACTED_COLUMNS = {"hashed_password", "imap_password", "key_hash"}


def _pk(table: str) -> str:
    return TABLE_PK.get(table, "id")


def _redact(row: dict) -> dict:
    return {k: ("***REDACTED***" if k in REDACTED_COLUMNS else v) for k, v in row.items()}


@router.get("/tables")
async def list_tables(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Return all allowed tables with their row counts."""
    results = []
    for table in sorted(ALLOWED_TABLES):
        count_result = await db.execute(text(f"SELECT COUNT(*) FROM {table}"))
        count = count_result.scalar()
        results.append({"table": table, "row_count": count, "pk_column": _pk(table)})
    return results


@router.get("/tables/{table_name}")
async def get_table_data(
    table_name: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Return paginated raw rows from a table."""
    if table_name not in ALLOWED_TABLES:
        raise HTTPException(status_code=404, detail=f"Table '{table_name}' not found or not allowed")

    offset = (page - 1) * page_size

    # Total count
    count_result = await db.execute(text(f"SELECT COUNT(*) FROM {table_name}"))
    total = count_result.scalar()

    # Rows
    rows_result = await db.execute(
        text(f"SELECT * FROM {table_name} ORDER BY 1 LIMIT :limit OFFSET :offset"),
        {"limit": page_size, "offset": offset},
    )
    columns = list(rows_result.keys())
    rows = [_redact(dict(zip(columns, row))) for row in rows_result.fetchall()]

    return {
        "table": table_name,
        "pk_column": _pk(table_name),
        "columns": columns,
        "rows": rows,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": (total + page_size - 1) // page_size,
    }


class DeleteRequest(BaseModel):
    ids: List[str]      # primary key values as strings (UUIDs, ints, or string keys)
    password: str       # admin's own password for confirmation


@router.delete("/tables/{table_name}")
async def delete_rows(
    table_name: str,
    body: DeleteRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Delete specific rows by primary key after verifying the admin's password."""
    if table_name not in ALLOWED_TABLES:
        raise HTTPException(status_code=404, detail=f"Table '{table_name}' not found or not allowed")

    if not body.ids:
        raise HTTPException(status_code=400, detail="No IDs provided")

    # Re-verify admin password
    result = await db.execute(select(User).where(User.id == current_user.id))
    user = result.scalar_one_or_none()
    if not user or not verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=403, detail="Incorrect password")

    pk_col = _pk(table_name)

    # Use IN (...) with individual bind params — safe parameterised query; table/pk_col from hardcoded whitelists
    try:
        placeholders = ",".join([f":id_{i}" for i in range(len(body.ids))])
        params = {f"id_{i}": v for i, v in enumerate(body.ids)}
        del_result = await db.execute(
            text(f"DELETE FROM {table_name} WHERE CAST({pk_col} AS TEXT) IN ({placeholders})"),
            params,
        )
        await db.commit()
        await log_audit(
            db, None, "system",
            f"DB Explorer: admin '{current_user.email}' deleted {del_result.rowcount} row(s) from '{table_name}'",
            {"table": table_name, "ids": body.ids, "deleted": del_result.rowcount},
        )
    except IntegrityError as e:
        await db.rollback()
        # Extract the referencing table name from the FK violation message if possible
        detail = str(e.orig)
        matches = re.findall(r'on table "([^"]+)"', detail)
        blocking = f'table "{matches[-1]}"' if matches else "a related table"
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot delete: one or more rows in '{table_name}' are still referenced by {blocking}. "
                f"Delete or nullify those dependent rows first."
            ),
        )

    return {"deleted": del_result.rowcount}
