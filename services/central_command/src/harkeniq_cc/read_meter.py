"""The machine read meter: ONE path, charged at the plane's one choke point.

A30.31 (A6-4B0c). A25.6 built the read meter -- a windowed counter, one
row per (tenant, agent, window), incremented atomically -- and A25.10 set
its rule: an authenticated machine request is charged to the agent its
TOKEN names, BEFORE anything is decided about the target, because a
refusal that costs nothing is an unbounded channel.

WHERE THE METER USED TO LIVE, AND WHY THAT WAS THE DEFECT
---------------------------------------------------------
It lived in handlers, inside the Operational Agent router. A handler can
only meter a request that reaches it, which left three holes the A6-4B0c
inventory measured on unmodified main:

* `GET /api/attention/`, `GET /api/incidents/` and `GET
  /api/incidents/{id}` joined the machine plane in A6-4A from two other
  routers, and no handler there charged anything -- served, 404 and 422
  were all free (F3);
* a malformed or invalid machine read was free on EVERY machine read
  route, the ones that meter included: FastAPI validates query parameters
  AFTER resolving dependencies and BEFORE calling the handler, so a 422
  never reached the charge on the handler's first line;
* the A25 completeness guard filtered one router's path prefix, so it
  could not see either.

WHERE IT LIVES NOW
------------------
In the route-surface guard (`api.deps.enforce_route_surface`), which every
CC route crosses through `require_permission` or `require_any_permission`
-- the one exception being the governed write, which is metered by its own
attempt ledger (A24.13) and consumes the surface decision inside it
(A29.15). A read of any on-plane route is therefore metered because it
crossed the guard, not because its author remembered, and the guard runs
to completion before query validation and before any handler line, which
makes A25.10's order a property of the framework rather than of each
handler.

This module is the meter itself, moved out of the Operational Agent router
UNCHANGED in substance: the bucket is still token-derived and still
chosen by nobody, the charge still owns its own transaction, and the
window is still computed exactly once and handed back. What is new is
`meter_machine_read`, the single entry point, and the only reason it is
not simply `_charge_machine_read`: 77 of the 98 declared routes declare
their guard twice, so the guard runs twice per request, and a request must
be charged once.

`route_contract.meter_census` proves every declared machine route crosses
this path, and a structural test pins the path itself:
`_charge_machine_read` has one caller, `meter_machine_read`, which has one
caller, the guard.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import HTTPException, Request

from harkeniq_cc.auth import UserContext

logger = logging.getLogger("harkeniq.cc.read_meter")

#: The attribute a charged request carries on its OWN state. Server-side
#: only: Starlette gives every request a fresh state, and nothing a caller
#: sends -- header, query, path or body -- can populate it.
_METERED = "harkeniq_machine_read_window"


async def meter_machine_read(
    request: Request, user: UserContext, *, job: str,
) -> datetime:
    """Charge this machine request ONE read, however often it is asked.

    THE ONE ENTRY POINT to the read meter. Idempotent per request, and
    that is its whole job: 77 of the 98 declared routes declare their guard
    both in `dependencies=[...]` and as the `user` parameter, and each
    `require_permission(p)` call returns a new closure, so FastAPI's
    per-request cache does not merge them and the guard -- and therefore
    this -- runs twice. The first call charges and remembers the window;
    every later call in the same request returns that window and charges
    nothing.

    The WINDOW is remembered, not a flag. A second guard invocation that
    lands on the far side of a minute boundary must not recompute one:
    recomputing is the race A29.16 closed one level down.

    `job` is a METRIC LABEL taken from the route's own declaration by the
    guard -- a bounded vocabulary, never a bucket. It cannot select whose
    allowance is spent: that is `_charge_machine_read`'s, from the token.

    Raises 429 when this agent's window is spent, exactly as before; no
    window is remembered then, because none was admitted -- and none needs
    to be, since the 429 ends the request before a second guard can ask.
    """
    from harkeniq_cc.metrics import record_machine_read_metered

    charged = getattr(request.state, _METERED, None)
    if charged is not None:
        return charged
    try:
        window = await _charge_machine_read(request, user)
    except HTTPException:
        # A 429. The durable counter DID move -- `admit_read` increments
        # before it compares -- so the request was metered, and the
        # service-level count keeps mirroring the durable one exactly.
        record_machine_read_metered(job)
        raise
    setattr(request.state, _METERED, window)
    record_machine_read_metered(job)
    return window


async def _charge_machine_read(
    request: Request, user: UserContext
) -> datetime:
    """Charge one status read to this agent's polling bucket, or 429.

    Shared by every machine read on the plane, so that alternating
    between the receipt endpoints, the proposal list, the agent detail,
    the runtime read, the dry-run, Attention and the incident reads cannot
    multiply the allowance -- which is exactly what an unmetered route
    would let a runtime do.

    Called ONLY by `meter_machine_read`. A30.31 moved it here from the
    Operational Agent router, unchanged in substance; a structural test
    fails the suite if anything else calls it.

    THE BUCKET IS SERVER-DERIVED, ALWAYS (MEDIUM). `user.tenant_id` and
    `user.user_id` come from the validated token and the
    `cc_agent_identities` row it resolved to; for a machine principal
    `user_id` IS the Operational Agent id. Nothing here reads the route's
    `agent_id`, the request body, a query value, a proposal id or any
    other caller-supplied identifier, so a caller cannot select whose
    allowance it spends -- which is what would turn a meter into a way to
    exhaust somebody else's.

    IT OWNS ITS OWN TRANSACTION (LOW). It used to take the request's
    session and commit it, which is a helper committing work it never
    knew about -- the mirror of the `session.rollback()` defect the
    counter itself already had. Accounting is not business state: it opens
    a short-lived session from the canonical sessionmaker, writes and
    commits ONLY the counter, and closes. The caller's transaction is
    neither committed nor rolled back, and this function holds no
    reference to it.
    """
    from harkeniq_cc.ingress_limits import READ_WINDOW_S, admit_read
    from harkeniq_cc.metrics import record_read_rate_limited, record_read_refusal

    tenant_id, agent_id = user.tenant_id, user.user_id
    sessionmaker = request.app.state.cc.sessionmaker
    async with sessionmaker() as accounting:
        # A29.16: `admit_read` computes the window ONCE and hands it back.
        # It is the only place on this path that may compute it -- see the
        # boundary note there -- and it is returned rather than taken as a
        # parameter so no caller can name the bucket it spends, which is
        # the property A25.9 asserts from this signature.
        permitted, used, window = await admit_read(
            accounting, tenant_id=tenant_id, agent_id=agent_id,
        )
        # Durable BEFORE anything downstream can raise. A charge that only
        # survived a successful read would make every refusal free -- a
        # 404-producing poll could then run unbounded, which is the same
        # hole A6-1 closed for authenticated submission refusals.
        await accounting.commit()
    if not permitted:
        record_read_rate_limited()
        record_read_refusal("rate_limited")
        raise HTTPException(
            status_code=429,
            detail=(
                f"this agent has made {used} status reads in the last "
                f"{READ_WINDOW_S} seconds"
            ),
        )
    # The window this charge actually landed on. Returned rather than
    # recomputed by the caller: that recomputation is the minute-boundary
    # race, and an annotation promising a datetime while the function
    # fell off its end returning None is how the race survived a fix that
    # claimed to close it.
    return window


async def record_surface_refusal_window(
    request: Request, user: UserContext, reason: str, window: datetime,
) -> None:
    """A29.16: durable, attributable, bounded refusal evidence.

    Extends the row `meter_machine_read` has already opened for this
    window rather than writing anywhere new, so the storage bound is
    unchanged: one row per (tenant, agent, window), whatever the traffic.

    Owns its own short-lived session for the reason A25.12 recorded --
    accounting is not business state and a helper must never commit or
    roll back a caller's transaction. Best effort: telemetry may not
    change behaviour, and a refusal that fails to record is still a
    refusal.
    """
    from harkeniq_cc.db.repos import AgentReadWindowRepo

    # `window` is REQUIRED and has no fallback on purpose. The charge
    # computed it and the evidence must land on that exact row; a
    # fallback would recompute, and recomputing IS the defect.
    #
    # `at` is a TIMESTAMP, not a bucket key, so taking it from the clock
    # here is safe -- it records when the refusal happened and selects
    # nothing.
    at = datetime.now(timezone.utc)
    try:
        sessionmaker = request.app.state.cc.sessionmaker
        async with sessionmaker() as accounting:
            await AgentReadWindowRepo(accounting).record_refusal(
                # Server-derived, from the validated token. Nothing here
                # reads the route's `agent_id`, the query or the body, so
                # a caller cannot choose whose window it marks.
                tenant_id=user.tenant_id,
                agent_id=user.user_id,
                window_start=window,
                reason=reason,
                at=at,
            )
            await accounting.commit()
    except Exception:  # noqa: BLE001 - accounting must not change behaviour
        # WARNING, not debug. This swallow is correct -- telemetry may not
        # change behaviour -- but it hid a `NameError` in this very
        # function through a full CI cycle: the evidence silently stopped
        # being written and every test that did not assert the counter
        # stayed green. A29.16 exists for operator observability, so an
        # observation that cannot be recorded is itself operator-visible.
        logger.warning("surface refusal not recorded", exc_info=True)
