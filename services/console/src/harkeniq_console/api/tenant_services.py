"""Tenant service placement — which L1-L3 stack serves which tenant.

Reads need ``tenant.view``, writes need ``tenant.manage``. Placement is
platform administration, so this router lives on the platform plane
(``/api/admin``) rather than under ``/api/tenants/{id}``: you register
where a tenant's stack is before anyone can enter that tenant's console.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_console.api.deps import get_session, require_permission
from harkeniq_console.auth import UserContext
from harkeniq_console.db.repos import AuditRepo, TenantRepo, TenantServiceRepo

router = APIRouter(prefix="/api/admin/tenant-services", tags=["tenant-services"])

# Only kinds the Console actually knows how to route. Rejecting unknown
# kinds keeps a typo from creating a placement nothing will ever read.
VALID_SERVICE_KINDS = ("central_command", "site_manager")


class RegisterServiceBody(BaseModel):
    service_kind: str = Field(..., description="central_command | site_manager")
    endpoint_url: str = Field(..., min_length=1, max_length=512)


def _service_dict(row) -> dict:
    return {
        "id": row.id,
        "tenant_id": row.tenant_id,
        "service_kind": row.service_kind,
        "endpoint_url": row.endpoint_url,
        "status": row.status,
        "registered_by": row.registered_by,
        "registered_at": row.registered_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
        "last_verified_at": (
            row.last_verified_at.isoformat() if row.last_verified_at else None
        ),
    }


@router.get("/{tenant_id}")
async def list_tenant_services(
    tenant_id: str,
    _user: UserContext = Depends(require_permission("tenant.view")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    rows = await TenantServiceRepo(session).list_by_tenant(tenant_id)
    return {"items": [_service_dict(r) for r in rows]}


@router.post("/{tenant_id}")
async def register_tenant_service(
    tenant_id: str,
    body: RegisterServiceBody,
    user: UserContext = Depends(require_permission("tenant.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    if body.service_kind not in VALID_SERVICE_KINDS:
        raise HTTPException(
            400, f"service_kind must be one of {VALID_SERVICE_KINDS}",
        )
    if await TenantRepo(session).get_by_id(tenant_id) is None:
        raise HTTPException(404, "tenant not found")

    row = await TenantServiceRepo(session).register(
        tenant_id=tenant_id,
        service_kind=body.service_kind,
        endpoint_url=body.endpoint_url,
        registered_by=user.user_id,
    )
    await AuditRepo(session).append(
        actor_id=user.user_id,
        actor_email=user.email,
        action="tenant_service.registered",
        subject_type="tenant_service",
        subject_id=row.id,
        tenant_id=tenant_id,
        detail={"kind": body.service_kind, "endpoint": body.endpoint_url},
    )
    await session.commit()
    return _service_dict(row)


@router.post("/{tenant_id}/{service_id}/disable")
async def disable_tenant_service(
    tenant_id: str,
    service_id: str,
    user: UserContext = Depends(require_permission("tenant.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    repo = TenantServiceRepo(session)
    row = await repo.get_by_id(service_id)
    if row is None or row.tenant_id != tenant_id:
        raise HTTPException(404, "service placement not found")
    await repo.disable(row)
    await AuditRepo(session).append(
        actor_id=user.user_id,
        actor_email=user.email,
        action="tenant_service.disabled",
        subject_type="tenant_service",
        subject_id=service_id,
        tenant_id=tenant_id,
        detail={"kind": row.service_kind},
    )
    await session.commit()
    return _service_dict(row)
