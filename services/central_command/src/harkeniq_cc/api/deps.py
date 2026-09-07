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


def has_permission(user: UserContext, permission: str) -> bool:
    """Does this principal hold this permission? ONE implementation.

    A26.7 needs to ask the question inside a handler rather than at the
    route guard -- the policy list stays open at `fleet.view` and it is
    the PROJECTION that varies with `governance.view`. The rule
    ("holds it, or holds the wildcard") already existed twice, inline, in
    the two guards below; it is extracted here so a handler asking the
    question cannot answer it differently from the guard that admitted
    the caller. There is no second permission model.
    """
    held = user.permissions
    return permission in held or "*" in held


# ---------------------------------------------------------------------------
# A29 (A6-4A): route-surface eligibility
# ---------------------------------------------------------------------------

#: Refusal reasons, closed. They reach the audit entry and the metric, and
#: A25.11 established that an unbounded reason set on a scrape surface is
#: a way to smuggle a tenant or agent id into an unauthenticated endpoint.
SURFACE_REFUSE_UNDECLARED = "surface_not_allowed"
SURFACE_REFUSE_NO_JOB = "machine_job_not_bound"
SURFACE_REFUSE_HUMAN = "machine_only_route"

SURFACE_REASONS = frozenset({
    SURFACE_REFUSE_UNDECLARED, SURFACE_REFUSE_NO_JOB, SURFACE_REFUSE_HUMAN,
})


def _route_key(request: Request) -> tuple[str, str]:
    """The templated route this request matched, or ("","") if unmatched."""
    route = request.scope.get("route")
    path = getattr(route, "path", "")
    return (request.method.upper(), path)


def evaluate_route_surface(
    request: Request, user: UserContext
) -> tuple[str, str]:
    """May this principal SPECIES use this product surface? (A29.4.)

    PURE. It reads the declaration and answers; it meters nothing, raises
    nothing and touches no database. Returns ``(reason, detail)``, and
    ``("", "")`` when the principal is eligible.

    THE ONE CANONICAL SOURCE OF ROUTE-SURFACE TRUTH. Both planes consume
    exactly this function -- the read plane through
    :func:`enforce_route_surface` below, the governed write through the
    handler, inside the attempt ledger A24.13 built. They cannot share a
    refusal HANDLER, because each meters in its own shape and folding them
    together would cost one of them its anti-amplification property. They
    can and must share the DECISION, or the declaration is authoritative
    for reads and decorative for the write.

    Asked before the permission question and answering a different one.
    Permission asks what a principal may ever hold; scope asks whether
    they hold it here; this asks whether the route is part of the product
    offered to this species at all.

    It can only EXCLUDE. A route declared MACHINE that the caller lacks
    the permission for is still refused by the permission guard -- nothing
    here admits anybody, which is what keeps it a product contract rather
    than a second authorization system.

    DEFAULT DENY (A29.3). A route absent from `MACHINE_SURFACE` is not
    machine-reachable, so a permission entering `MACHINE_PRINCIPAL_CEILING`
    opens nothing on its own. That is the property this exists for; the
    narrowing is the occasion, not the point.
    """
    from harkeniq_cc.machine_identity import is_machine
    from harkeniq_cc.route_contract import (
        SURFACE_HUMAN, SURFACE_MACHINE, machine_surface,
    )

    method, path = _route_key(request)
    surface, job = machine_surface(method, path)

    if not is_machine(user):
        if surface == SURFACE_MACHINE:
            return (
                SURFACE_REFUSE_HUMAN,
                "this is a machine-principal surface; a person reads these "
                "facts through the Console",
            )
        return "", ""

    if surface == SURFACE_HUMAN:
        return (
            SURFACE_REFUSE_UNDECLARED,
            "this route is not part of the External Agent API plane",
        )
    if job not in getattr(user, "machine_jobs", frozenset()):
        # A29.6: the BINDING decides, not the permission. An agent whose
        # operator did not bind this job is refused even though the
        # ceiling admits the permission the route demands.
        return SURFACE_REFUSE_NO_JOB, f"this agent holds no {job!r} binding"
    return "", ""


async def enforce_route_surface(request: Request, user: UserContext) -> None:
    """The READ plane's handler for :func:`evaluate_route_surface`.

    A25.10: AN AUTHENTICATED REFUSAL IS NOT FREE. The request is charged
    to the agent the TOKEN names before it is refused, so a runtime cannot
    probe the plane without spending its own allowance. Charged here
    rather than in the handler because a refused request never reaches
    one -- and only on refusal, so a served request is still metered
    exactly once, by the handler.

    The read window is also the BOUND (A27.13's rule): it is a windowed
    counter, so a flood costs one row per minute and then 429s. The same
    row now carries bounded, attributable refusal evidence (A29.16) --
    still one row per (tenant, agent, window), never one per request.
    """
    from harkeniq_cc.metrics import record_surface_refusal

    reason, detail = evaluate_route_surface(request, user)
    if not reason:
        return

    from harkeniq_cc.machine_identity import is_machine

    if is_machine(user):
        from harkeniq_cc.api.operational_agents import (
            _charge_machine_read, _record_surface_refusal_window,
        )

        # ONE WINDOW for the whole refusal: the charge opens the row and
        # returns which one, and the evidence updates THAT row. Computing
        # it twice would straddle a minute boundary and drop the evidence.
        window = None
        try:
            window = await _charge_machine_read(request, user)
        except HTTPException as exhausted:
            # Already over its polling allowance: 429 is the truer answer,
            # and it is what stops the probe.
            if exhausted.status_code == 429:
                raise
        # A29.16: durable, attributable, and bounded by the SAME window --
        # a process-local counter cannot tell an operator WHICH runtime is
        # misconfigured, and a row per refusal would be the amplifier
        # A24.13 and A27.13 both refused.
        await _record_surface_refusal_window(
            request, user, reason, window=window,
        )
    record_surface_refusal(reason)
    raise HTTPException(status_code=403, detail=detail)


def require_permission(permission: str):
    """Return a dependency that checks the user has a specific permission."""

    async def _check(
        request: Request,
        user: UserContext = Depends(get_current_user),
    ) -> UserContext:
        # A29.4: species eligibility first, then the unchanged permission
        # question. Ordering matters only for the message a caller sees;
        # neither can admit anybody the other refuses.
        await enforce_route_surface(request, user)
        if not has_permission(user, permission):
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

    async def _check(
        request: Request,
        user: UserContext = Depends(get_current_user),
    ) -> UserContext:
        await enforce_route_surface(request, user)
        if any(has_permission(user, p) for p in permissions):
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
            # A23-4: the token's email claim is an authenticated alias of
            # this subject. A legacy grant keyed by it is prior evidence
            # (no synthesis), and still authorizes nothing.
            aliases=[user.email] if user.email else (),
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
