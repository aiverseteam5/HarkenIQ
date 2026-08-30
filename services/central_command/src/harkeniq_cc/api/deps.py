"""Shared FastAPI dependencies."""

from __future__ import annotations

from typing import AsyncGenerator

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_cc.auth import UserContext, get_current_user  # noqa: F401


def get_cc_state(request: Request):
    return request.app.state.cc


def require_role(*roles: str):
    """Return a dependency that checks the user has one of the given roles."""

    async def _check(user: UserContext = Depends(get_current_user)) -> UserContext:
        if user.role not in roles and "*" not in user.permissions:
            raise HTTPException(
                status_code=403,
                detail=f"requires one of roles: {', '.join(roles)}",
            )
        return user

    return _check


def require_permission(permission: str):
    """Return a dependency that checks the user has a specific permission."""

    async def _check(user: UserContext = Depends(get_current_user)) -> UserContext:
        if permission not in user.permissions and "*" not in user.permissions:
            raise HTTPException(
                status_code=403,
                detail=f"missing permission: {permission}",
            )
        return user

    return _check


def require_any_permission(*permissions: str):
    """Allow a caller holding ANY of these permissions (E0.3, A13).

    Some reads are legitimately reachable by two different personas for
    two different reasons: an operator reads approval history because
    they work the queue, an auditor reads it because it is the evidence
    R-C3 promises. Rather than invent a third permission -- the
    vocabulary is fixed -- the guard accepts either.

    Deliberately read-only. Every mutation keeps its single, specific
    permission: D2 forbids broadening mutation permissions, and an
    `any-of` gate on a write would be exactly that.
    """

    async def _check(user: UserContext = Depends(get_current_user)) -> UserContext:
        held = set(user.permissions)
        if "*" in held or held.intersection(permissions):
            return user
        raise HTTPException(
            status_code=403,
            detail=f"requires one of: {', '.join(sorted(permissions))}",
        )

    return _check


async def get_scope(
    request: Request,
    user: UserContext = Depends(get_current_user),
):
    """Resolve the caller's authorization scope for this request (E1.2).

    A separate dependency rather than a field on `UserContext`, for two
    reasons that are both load-bearing:

    * `get_current_user` has no database session, and scope lives in the
      database.
    * `permission_subset` is PER GRANT, so the effective permission is
      **object-dependent** -- a principal may hold `site.manage` over one
      cluster and read-only over another. There is no single correct set
      to put on the context, which is exactly why the route guard cannot
      be the place a subset is enforced.

    The route guard still answers "could this actor ever hold this
    permission". `ResolvedScope.permits(permission, target)` answers
    "does this actor hold it HERE", and only the second one decides.
    """
    from harkeniq_cc.governance import load_scope
    from harkeniq_cc.auth import ROLE_PERMISSIONS

    state = request.app.state.cc
    async with state.sessionmaker() as session:
        return await load_scope(
            session,
            tenant_id=user.tenant_id,
            principal_ref=user.user_id,
            role_permissions=ROLE_PERMISSIONS.get(
                user.role, list(user.permissions)
            ) or list(user.permissions),
        )


def forbid_out_of_scope(
    scope,
    permission: str,
    *,
    what: str,
    **target,
) -> None:
    """Raise 403 unless the caller holds `permission` over this target.

    The object gate, layer 3. Called with the object already resolved,
    so the refusal names the object rather than the route.
    """
    if scope.permits(permission, **target):
        return
    raise HTTPException(
        status_code=403,
        detail=(
            f"{what} is outside your authorized scope: you do not hold "
            f"{permission!r} over it"
        ),
    )


async def get_session(request: Request) -> AsyncGenerator[AsyncSession, None]:
    """Yield an AsyncSession scoped to one request."""
    state = request.app.state.cc
    async with state.sessionmaker() as session:
        yield session
