"""Tenant management endpoints (super admin only)."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession

# Listing a tenant and entering one are different acts. Reading the
# registry is `tenant.view` — which platform_support holds, and was refused
# for as long as this router was blanket require_super_admin. Entering a
# tenant is still governed by tenant_scope plus a support-access grant, so
# widening the read does not widen anyone's reach into tenant data.
from harkeniq_console.api.deps import get_session, require_permission
from harkeniq_console.auth import UserContext
from harkeniq_console.db.repos import TenantRepo
from harkeniq_console.tenant_service import TenantCreateRequest, TenantError, TenantService

router = APIRouter(prefix="/api/admin/tenants", tags=["tenants"])


class CreateTenantBody(BaseModel):
    name: str
    slug: str
    billing_country: str
    currency: str = "USD"
    plan: str = "approve"
    node_commit: int = 0
    admin_email: str = ""


class UpdateTenantBody(BaseModel):
    name: Optional[str] = None
    billing_country: Optional[str] = None
    currency: Optional[str] = None


class SuspendBody(BaseModel):
    reason: str


def _tenant_dict(t) -> dict:
    return {
        "id": t.id,
        "slug": t.slug,
        "name": t.name,
        "status": t.status,
        "billing_country": t.billing_country,
        "currency": t.currency,
        "keycloak_realm": t.keycloak_realm,
        "created_at": t.created_at.isoformat(),
        "updated_at": t.updated_at.isoformat(),
        "suspended_at": t.suspended_at.isoformat() if t.suspended_at else None,
    }


@router.post("/")
async def create_tenant(
    body: CreateTenantBody,
    user: UserContext = Depends(require_permission("tenant.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    svc = TenantService(session)
    req = TenantCreateRequest(
        name=body.name,
        slug=body.slug,
        billing_country=body.billing_country,
        currency=body.currency,
        plan=body.plan,
        node_commit=body.node_commit,
        admin_email=body.admin_email,
    )
    try:
        result = await svc.create_tenant(req, created_by=user.email)
        await session.commit()
        return result
    except TenantError as exc:
        status = 409 if exc.code == "conflict" else 400
        raise HTTPException(status_code=status, detail=str(exc))


@router.get("/")
async def list_tenants(
    search: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    _user: UserContext = Depends(require_permission("tenant.view")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    items, total = await TenantRepo(session).list_filtered(
        search=search, status=status, page=page, page_size=page_size,
    )
    return {
        "items": [_tenant_dict(t) for t in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/{tenant_id}")
async def get_tenant(
    tenant_id: str,
    _user: UserContext = Depends(require_permission("tenant.view")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    svc = TenantService(session)
    detail = await svc.get_tenant_detail(tenant_id)
    if not detail:
        raise HTTPException(status_code=404, detail="tenant not found")
    return detail


@router.patch("/{tenant_id}")
async def update_tenant(
    tenant_id: str,
    body: UpdateTenantBody,
    user: UserContext = Depends(require_permission("tenant.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    repo = TenantRepo(session)
    tenant = await repo.get_by_id(tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="tenant not found")
    updates = body.model_dump(exclude_unset=True)
    if updates:
        await repo.update(tenant, **updates)
        await session.commit()
    return _tenant_dict(tenant)


@router.post("/{tenant_id}/suspend")
async def suspend_tenant(
    tenant_id: str,
    body: SuspendBody,
    user: UserContext = Depends(require_permission("tenant.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    svc = TenantService(session)
    try:
        result = await svc.suspend_tenant(tenant_id, body.reason, user.email)
        await session.commit()
        return result
    except TenantError as exc:
        status = 404 if exc.code == "not_found" else 409
        raise HTTPException(status_code=status, detail=str(exc))


@router.post("/{tenant_id}/reactivate")
async def reactivate_tenant(
    tenant_id: str,
    user: UserContext = Depends(require_permission("tenant.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    svc = TenantService(session)
    try:
        result = await svc.reactivate_tenant(tenant_id, user.email)
        await session.commit()
        return result
    except TenantError as exc:
        status = 404 if exc.code == "not_found" else 409
        raise HTTPException(status_code=status, detail=str(exc))
