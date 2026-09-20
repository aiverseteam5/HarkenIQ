"""Autonomy API: the governed decision boundary for action.

S5 (2026-08-29). One read that answers, for every action class in this
tenant: may it run without a human, on what evidence, under what live
safety state, and what would change that.

This router only FETCHES tenant-scoped inputs and hands them to the pure
composer in `harkeniq_cc.autonomy`; all judgement lives there and is
unit-testable without a database.

Governance
----------
`fleet.view` — the posture read-split ratified as D2 and landed in S1:
the people living under the trust ladder can see it. Every mutation
stays where it already is, at `site.manage` on `/api/policies/*`. S5
adds no mutation endpoint and broadens no permission.

**This contract confers no authority.** `disposition: "autonomous"` is a
prediction an actor may plan with, not a grant. Execution still runs the
unchanged node funnel: allow-list, preconditions, stop switch, lease,
blast radius. The Console is this contract's first consumer; the
Operational Agent (A0/A1) is its second and gets nothing extra.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_cc.api.deps import get_scope, get_session, require_permission
from harkeniq_cc.auth import UserContext
from harkeniq_cc.scope import read_reach
from harkeniq_cc.governance import load_autonomy_contract

router = APIRouter(prefix="/api/autonomy", tags=["autonomy"])


@router.get(
    "/",
    dependencies=[Depends(require_permission("fleet.view"))],
)
async def autonomy_contract(
    site_id: str | None = Query(None, description="restrict to one site"),
    action_type: str | None = Query(None, description="one action class"),
    user: UserContext = Depends(require_permission("fleet.view")),
    session: AsyncSession = Depends(get_session),
    scope=Depends(get_scope),
) -> dict:
    """The autonomy contract AS THIS CALLER MAY READ IT (A30.26).

    Tenant-owned posture -- the level, the ladder, the tenant stop switch,
    each class's risk and grant level, the approval policy -- is the same
    for every reader. Every SITE-derived fact is composed over the sites
    the caller's `fleet.view` reach authorizes and over no other: the
    safety lists, the error budgets and their totals, `reported`, the
    outcome evidence, and the disposition, reason, approval requirement
    and advancement folded from them. The sites are selected BEFORE the
    fold, so there is no tenant-wide value behind the answer -- a
    cluster-scoped principal reads the contract of a tenant that contains
    only their cluster.

    `site_id` narrows WITHIN that reach and can never widen it. Asking for
    a site outside it composes over nothing, which is also what a site id
    that does not exist gets.
    """
    return await load_autonomy_contract(
        session,
        tenant_id=user.tenant_id,
        actor_id=f"user:{user.user_id}",
        actor_species="human",
        permissions=user.permissions,
        reach=read_reach(scope, "fleet.view"),
        site_id=site_id,
        action_type=action_type,
    )
