"""Audit log endpoints (tenant-scoped + admin platform-wide)."""

from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_console.api.deps import get_session, require_super_admin, tenant_scope, require_permission
from harkeniq_console.auth import UserContext, get_current_user
from harkeniq_console.db.repos import AuditRepo

router = APIRouter(prefix="/api/tenants/{tenant_id}/audit", tags=["audit"])
admin_router = APIRouter(prefix="/api/admin/audit", tags=["audit-admin"])


def _entry_dict(e) -> dict:
    return {
        "id": e.id,
        "ts": e.ts.isoformat() if e.ts else None,
        "actor_id": e.actor_id,
        "actor_email": e.actor_email,
        "action": e.action,
        "subject_type": e.subject_type,
        "subject_id": e.subject_id,
        "tenant_id": e.tenant_id,
        "detail": e.detail,
        "seq": e.seq,
        "prev_hash": e.prev_hash,
        "entry_hash": e.entry_hash,
    }


# ── tenant-scoped ───────────────────────────────────────────────────


@router.get("/")
async def list_audit_logs(
    tenant_id: str,
    actor: str | None = None,
    action: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    search: str | None = None,
    page: int = 1,
    page_size: int = 50,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(tenant_scope),
) -> dict:
    df = datetime.combine(date_from, datetime.min.time()) if date_from else None
    dt = datetime.combine(date_to, datetime.max.time()) if date_to else None
    items, total = await AuditRepo(session).list_filtered(
        tenant_id=tenant_id, actor=actor, action=action,
        date_from=df, date_to=dt, search=search,
        page=page, page_size=page_size,
    )
    return {"items": [_entry_dict(e) for e in items], "total": total, "page": page}


@router.get("/export")
async def export_audit_logs(
    tenant_id: str,
    format: str = "csv",
    date_from: date | None = None,
    date_to: date | None = None,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(tenant_scope),
) -> StreamingResponse:
    df = datetime.combine(date_from, datetime.min.time()) if date_from else None
    dt = datetime.combine(date_to, datetime.max.time()) if date_to else None
    items, _ = await AuditRepo(session).list_filtered(
        tenant_id=tenant_id, date_from=df, date_to=dt,
        page=1, page_size=10000,
    )
    entries = [_entry_dict(e) for e in items]

    if format == "json":
        content = json.dumps(entries, indent=2)
        return StreamingResponse(
            io.BytesIO(content.encode()),
            media_type="application/json",
            headers={"Content-Disposition": f"attachment; filename=audit-{tenant_id}.json"},
        )

    # CSV
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf,
        fieldnames=["id", "ts", "actor_id", "actor_email", "action", "subject_type", "subject_id", "detail", "seq", "prev_hash", "entry_hash"],
    )
    writer.writeheader()
    for e in entries:
        row = {k: e.get(k, "") for k in writer.fieldnames}
        row["detail"] = json.dumps(e.get("detail")) if e.get("detail") else ""
        writer.writerow(row)
    return StreamingResponse(
        io.BytesIO(buf.getvalue().encode()),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=audit-{tenant_id}.csv"},
    )


# ── admin (platform-wide) ───────────────────────────────────────────


@admin_router.get("/verify")
async def admin_verify_audit_chain(
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_super_admin),
) -> dict:
    """Verify the SHA-256 audit hash chain (R4-2 P12, OQ-20: on-demand)."""
    result = await AuditRepo(session).verify_chain()
    return {
        "valid": result.valid,
        "length": result.length,
        "first_bad_seq": result.first_bad_seq,
        "error": result.error,
    }


@admin_router.get("/")
async def admin_list_audit(
    tenant_id: str | None = None,
    actor: str | None = None,
    action: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    search: str | None = None,
    page: int = 1,
    page_size: int = 50,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_super_admin),
) -> dict:
    df = datetime.combine(date_from, datetime.min.time()) if date_from else None
    dt = datetime.combine(date_to, datetime.max.time()) if date_to else None
    items, total = await AuditRepo(session).list_filtered(
        tenant_id=tenant_id, actor=actor, action=action,
        date_from=df, date_to=dt, search=search,
        page=page, page_size=page_size,
    )
    return {"items": [_entry_dict(e) for e in items], "total": total, "page": page}


@admin_router.get("/export")
async def admin_export_audit(
    format: str = "csv",
    tenant_id: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_super_admin),
) -> StreamingResponse:
    df = datetime.combine(date_from, datetime.min.time()) if date_from else None
    dt = datetime.combine(date_to, datetime.max.time()) if date_to else None
    items, _ = await AuditRepo(session).list_filtered(
        tenant_id=tenant_id, date_from=df, date_to=dt,
        page=1, page_size=10000,
    )
    entries = [_entry_dict(e) for e in items]

    if format == "json":
        content = json.dumps(entries, indent=2)
        return StreamingResponse(
            io.BytesIO(content.encode()),
            media_type="application/json",
            headers={"Content-Disposition": "attachment; filename=audit-platform.json"},
        )

    buf = io.StringIO()
    writer = csv.DictWriter(
        buf,
        fieldnames=["id", "ts", "actor_id", "actor_email", "action", "subject_type", "subject_id", "tenant_id", "detail", "seq", "prev_hash", "entry_hash"],
    )
    writer.writeheader()
    for e in entries:
        row = {k: e.get(k, "") for k in writer.fieldnames}
        row["detail"] = json.dumps(e.get("detail")) if e.get("detail") else ""
        writer.writerow(row)
    return StreamingResponse(
        io.BytesIO(buf.getvalue().encode()),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=audit-platform.csv"},
    )
