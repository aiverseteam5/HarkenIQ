"""Shared FastAPI dependencies."""

from __future__ import annotations

from typing import Callable

from fastapi import Depends, HTTPException, Request

from harkeniq_console.auth import UserContext, get_current_user
from harkeniq_console.db.repos import SupportAccessLogRepo
from harkeniq_console.permissions import has_permission


def get_console_state(request: Request):
    return request.app.state.console


async def get_session(request: Request):
    """Yield an AsyncSession from the runtime sessionmaker."""
    state = request.app.state.console
    async with state.sessionmaker() as session:
        yield session


def require_role(*roles: str) -> Callable:
    """Dependency that checks the authenticated user has one of *roles*."""
    async def _check(user: UserContext = Depends(get_current_user)) -> UserContext:
        if user.role not in roles:
            raise HTTPException(
                status_code=403,
                detail=f"requires one of roles: {', '.join(roles)}",
            )
        return user
    return _check


def require_permission(permission: str) -> Callable:
    """Dependency that checks the authenticated user has *permission*."""
    async def _check(user: UserContext = Depends(get_current_user)) -> UserContext:
        if not has_permission(user.role, permission, user.permissions):
            raise HTTPException(status_code=403, detail=f"missing permission: {permission}")
        return user
    return _check


def require_platform_permission(permission: str) -> Callable:
    """A platform-plane guard: platform realm AND the permission.

    Review finding (4 independent passes): guarding a platform endpoint with
    ``require_permission`` alone leaks it to customers, because the atomic
    permissions are shared vocabulary — ``tenant.view`` is held by
    ``tenant_owner`` (and sits inside the custom-role ceiling), so a bare
    permission check let any tenant owner read the vendor's tenant registry
    and every tenant's Central Command endpoint. Platform-plane routes ask
    two questions: is this vendor staff, and does their role grant it.
    """
    async def _check(user: UserContext = Depends(get_current_user)) -> UserContext:
        if not user.is_platform_user:
            raise HTTPException(
                status_code=403, detail="platform credentials required"
            )
        if not has_permission(user.role, permission, user.permissions):
            raise HTTPException(status_code=403, detail=f"missing permission: {permission}")
        return user
    return _check


async def require_super_admin(
    user: UserContext = Depends(get_current_user),
) -> UserContext:
    """Shortcut: only platform_super_admin may proceed."""
    if user.role != "platform_super_admin":
        raise HTTPException(status_code=403, detail="requires platform_super_admin")
    return user


async def tenant_scope(
    tenant_id: str,
    user: UserContext = Depends(get_current_user),
    session=Depends(get_session),
) -> UserContext:
    """Validate that the caller may reach *tenant_id* at all.

    Answers "which tenant?" only. It deliberately does NOT answer "are you
    allowed?" — compose it with :func:`require_tenant_permission` for that.
    Routes guarded by this alone are reachable by every role in the tenant.

    Crossing the platform/tenant boundary used to be free: any platform
    user passed unconditionally, so ``platform_support`` could reach every
    tenant with no elevation and no approval. ``SupportAccessLog`` already
    modelled exactly the grant that should govern that — time-bound,
    revocable, attributable, audited at enable and revoke — but nothing in
    any authorization path consulted it. It does now.

    ``platform_super_admin`` keeps an unconditional path on purpose: it is
    the break-glass, and gating it on the grant mechanism would mean a
    failure of that mechanism locks everyone out mid-incident.
    """
    if user.is_platform_user:
        if user.role == "platform_super_admin":
            return user
        active = await SupportAccessLogRepo(session).get_active(tenant_id)
        if active is None:
            raise HTTPException(
                status_code=403,
                detail="support access not enabled for this tenant",
            )
        return user
    if user.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="tenant scope mismatch")
    return user


def require_tenant_permission(permission: str) -> Callable:
    """Tenant membership *and* the permission the route needs.

    ``tenant_scope`` checks membership only, so whether a permission was
    enforced at all used to depend on the route body remembering to call
    ``has_permission`` — and five of the seven tenant routers did not. The
    only thing refusing an unentitled caller was the SPA declining to draw
    the button, which inverts spec S4 ("enforced server-side, not just
    hidden in the UI"): a `viewer` could mint tenant API keys, pay an
    invoice, and export the audit log by calling the endpoint directly.

    Reaching the tenant at all is ``tenant_scope``'s question, and for a
    platform user it is governed by a support-access grant. This is the
    separate question of what the caller may do once inside, and it is
    asked of platform users too: a grant admits ``platform_support``, it
    does not make it root. ``platform_super_admin`` holds every permission
    by definition, so its break-glass is unaffected.
    """
    async def _check(user: UserContext = Depends(tenant_scope)) -> UserContext:
        if not has_permission(user.role, permission, user.permissions):
            raise HTTPException(
                status_code=403, detail=f"missing permission: {permission}"
            )
        return user
    return _check
