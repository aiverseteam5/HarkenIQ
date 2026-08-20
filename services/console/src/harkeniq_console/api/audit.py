"""Audit log endpoints."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/tenants/{tenant_id}/audit", tags=["audit"])


@router.get("/")
async def list_audit_logs(tenant_id: str) -> dict:
    return {"stub": "list_audit_logs", "tenant_id": tenant_id}


@router.get("/export")
async def export_audit_logs(tenant_id: str) -> dict:
    return {"stub": "export_audit_logs", "tenant_id": tenant_id}


# ── admin audit ────────────────────────────────────────────────────

admin_router = APIRouter(prefix="/api/admin/audit", tags=["audit-admin"])


@admin_router.get("/")
async def admin_list_audit() -> dict:
    return {"stub": "admin_list_audit"}


@admin_router.get("/export")
async def admin_export_audit() -> dict:
    return {"stub": "admin_export_audit"}
