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

from urllib.parse import urlparse

from harkeniq_console.api.deps import get_session, require_platform_permission
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
    }


def _validate_endpoint_url(url: str) -> None:
    """Refuse URLs the proxy should never be pointed at.

    The proxy forwards the caller's bearer token to this URL, so an
    arbitrary string here is an SSRF-and-token-exfiltration edge even
    though registration is super-admin-gated. http is allowed because
    sovereign in-cluster deployments terminate TLS elsewhere; userinfo
    and missing hosts are never legitimate.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(400, "endpoint_url must be http:// or https://")
    if not parsed.hostname:
        raise HTTPException(400, "endpoint_url must include a host")
    if parsed.username or parsed.password:
        raise HTTPException(400, "endpoint_url must not carry credentials")


@router.get("/{tenant_id}")
async def list_tenant_services(
    tenant_id: str,
    _user: UserContext = Depends(require_platform_permission("tenant.view")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    # Same rule as tenant_scope: an unknown tenant is a 404, never an empty
    # list — "no placements" and "no such tenant" must not look identical.
    if await TenantRepo(session).get_by_id(tenant_id) is None:
        raise HTTPException(404, "tenant not found")
    rows = await TenantServiceRepo(session).list_by_tenant(tenant_id)
    return {"items": [_service_dict(r) for r in rows]}


@router.post("/{tenant_id}")
async def register_tenant_service(
    tenant_id: str,
    body: RegisterServiceBody,
    user: UserContext = Depends(require_platform_permission("tenant.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    if body.service_kind not in VALID_SERVICE_KINDS:
        raise HTTPException(
            400, f"service_kind must be one of {VALID_SERVICE_KINDS}",
        )
    if await TenantRepo(session).get_by_id(tenant_id) is None:
        raise HTTPException(404, "tenant not found")
    _validate_endpoint_url(body.endpoint_url)

    # One tenant -> one CC is the architectural invariant (spec §3, decided
    # by Vinod 2026-08-28): CC has no per-tenant data filtering, so two
    # tenants sharing an endpoint would silently serve one tenant's fleet
    # under the other's URL. Shared CC, if ever wanted, is a separate
    # architecture decision — not a side effect of registration.
    repo = TenantServiceRepo(session)
    conflict = await repo.find_active_by_endpoint(body.endpoint_url)
    if conflict is not None and conflict.tenant_id != tenant_id:
        raise HTTPException(
            409,
            "endpoint already registered to another tenant — one tenant, "
            "one Central Command",
        )

    row = await repo.register(
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
    user: UserContext = Depends(require_platform_permission("tenant.manage")),
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
