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
from harkeniq_cc.autonomy import narrow_to_sites
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
    """The tenant's autonomy contract: posture, evidence, safety, advancement.

    Every read below is tenant-scoped by its repository; `site_id`
    narrows within the tenant and can never widen beyond it. A23: the
    disposition is tenant posture and every reader sees it; the lists
    that NAME sites (safety, blocking conditions) are narrowed to the
    caller's reach, so a cluster-scoped principal learns nothing about
    sites outside it.
    """
    contract = await load_autonomy_contract(
        session,
        tenant_id=user.tenant_id,
        actor_id=f"user:{user.user_id}",
        actor_species="human",
        permissions=user.permissions,
        site_id=site_id,
        action_type=action_type,
    )
    visible = None if getattr(scope, "tenant_wide", False) else set(scope.site_ids)
    return narrow_to_sites(contract, visible)
