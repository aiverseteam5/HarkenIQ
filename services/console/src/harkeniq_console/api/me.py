"""Current-user identity and effective permissions.

Spec S4: "Permission checks are enforced server-side per request; the UI
only reflects them." The Console SPA used to derive its own view of the
user by decoding the access token, which failed three ways at once: it
read realm_access.roles while Keycloak mints realm_roles, it filtered for
an "hiq_" prefix the roles do not carry, and no permissions / tenant_id /
is_platform_user claim is minted at all. Every user rendered as "viewer",
including the platform super admin.

Serving the answer from here keeps one source of truth (the same
ROLE_PERMISSIONS the request guards use), picks up custom-role bundles for
free, and needs no realm-mapper change.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_console.api.deps import get_current_user, get_session
from harkeniq_console.db.models import Tenant
from harkeniq_console.auth import UserContext
from harkeniq_console.permissions import PERMISSIONS, ROLE_PERMISSIONS

router = APIRouter(prefix="/api/me", tags=["me"])


@router.get("")
@router.get("/")
async def whoami(user: UserContext = Depends(get_current_user)) -> dict:
    """Identity plus the caller's full effective permission set."""
    granted = set(ROLE_PERMISSIONS.get(user.role, set()))
    granted.update(p for p in user.permissions if p in PERMISSIONS)
    return {
        "user_id": user.user_id,
        "email": user.email,
        "role": user.role,
        "tenant_id": user.tenant_id,
        "is_platform_user": user.is_platform_user,
        "permissions": sorted(granted),
    }


@router.get("/tenants")
async def selectable_tenants(
    user: UserContext = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Tenants this caller may act as.

    Tenant users get exactly their own tenant — this is how the tenant
    context header resolves the tenant NAME for roles that do not hold
    tenant.view (most of them), without opening the platform registry to
    customers. Platform users get the active list (programmatic parity
    with the registry they can already read).
    """
    if user.is_platform_user:
        rows = (
            await session.execute(
                select(Tenant.id, Tenant.name, Tenant.slug)
                .where(Tenant.status == "active")
                .order_by(Tenant.name)
            )
        ).all()
    elif user.tenant_id:
        rows = (
            await session.execute(
                select(Tenant.id, Tenant.name, Tenant.slug)
                .where(Tenant.id == user.tenant_id)
            )
        ).all()
    else:
        rows = []
    return {
        "tenants": [{"id": r[0], "name": r[1], "slug": r[2]} for r in rows],
        "selectable": user.is_platform_user,
    }
