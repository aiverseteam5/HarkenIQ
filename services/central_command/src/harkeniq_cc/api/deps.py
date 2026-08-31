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
    from harkeniq_cc.governance import PRINCIPAL_AGENT, load_scope
    from harkeniq_cc.auth import ROLE_PERMISSIONS
    from harkeniq_cc.machine_identity import is_machine

    state = request.app.state.cc

    # A3 (spec A20): an authenticated Operational Agent resolves through
    # the SAME resolver, as the principal it already is -- its grants are
    # `cc_scope_grants` rows with principal_type="agent", keyed on the
    # agent id. Two things differ from a human, and both are deliberate:
    #
    #   * `role_permissions` is the A20.3 ceiling intersection carried on
    #     the context, NEVER a role and NEVER ["*"]. `load_agent_scope`
    #     passes ["*"] for the in-process evaluator because that path only
    #     ever asks WHERE; over HTTP the same value would satisfy every
    #     route guard in the platform, including action.approve.
    #   * agent grants carry no realm (an agent id is a CC row id, not a
    #     realm subject), so they are not narrowed by one.
    if is_machine(user):
        async with state.sessionmaker() as session:
            return await load_scope(
                session,
                tenant_id=user.tenant_id,
                principal_ref=user.user_id,
                role_permissions=list(user.permissions),
                principal_type=PRINCIPAL_AGENT,
                realm="",
            )

    async with state.sessionmaker() as session:
        return await load_scope(
            session,
            tenant_id=user.tenant_id,
            principal_ref=user.user_id,
            # E1.4: only grants made under the realm this Central Command
            # serves. A subject id from another realm is a different
            # person, or nobody.
            realm=getattr(state.config, "keycloak_realm", "") or "",
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
