"""Tenant management endpoints."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/admin/tenants", tags=["tenants"])


@router.post("/")
async def create_tenant() -> dict:
    return {"stub": "create_tenant"}


@router.get("/")
async def list_tenants() -> dict:
    return {"stub": "list_tenants"}


@router.get("/{tenant_id}")
async def get_tenant(tenant_id: str) -> dict:
    return {"stub": "get_tenant", "tenant_id": tenant_id}


@router.patch("/{tenant_id}")
async def update_tenant(tenant_id: str) -> dict:
    return {"stub": "update_tenant", "tenant_id": tenant_id}


@router.post("/{tenant_id}/suspend")
async def suspend_tenant(tenant_id: str) -> dict:
    return {"stub": "suspend_tenant", "tenant_id": tenant_id}


@router.post("/{tenant_id}/reactivate")
async def reactivate_tenant(tenant_id: str) -> dict:
    return {"stub": "reactivate_tenant", "tenant_id": tenant_id}
