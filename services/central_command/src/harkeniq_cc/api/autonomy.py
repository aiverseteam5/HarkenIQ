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

from harkeniq_cc.api.deps import get_session, require_permission
from harkeniq_cc.auth import UserContext
from harkeniq_cc.autonomy import build_autonomy
from harkeniq_cc.db.repos import (
    ApprovalPolicyRepo,
    AutonomyBudgetRepo,
    LearnedSignalRepo,
    OutcomeHistoryRepo,
    SafetyStateRepo,
    SiteRepo,
    StopSwitchRepo,
)

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
) -> dict:
    """The tenant's autonomy contract: posture, evidence, safety, advancement.

    Every read below is tenant-scoped by its repository; `site_id`
    narrows within the tenant and can never widen beyond it.
    """
    tenant_id = user.tenant_id

    budgets = await AutonomyBudgetRepo(session).list_all(tenant_id)
    stop_switch = await StopSwitchRepo(session).get(tenant_id)
    outcomes = await OutcomeHistoryRepo(session).list_outcome_dicts(tenant_id)
    safety_rows = await SafetyStateRepo(session).list_for_tenant(tenant_id)
    sites = await SiteRepo(session).list_all(tenant_id)
    learned = await LearnedSignalRepo(session).list_active(tenant_id)
    # Approval policies shape what "requires approval" actually means for
    # a class. Read at fleet.view here even though managing them needs
    # site.manage: knowing an action needs two approvers is posture, and
    # the posture read-split (D2) is the whole point of this surface.
    policies = await ApprovalPolicyRepo(session).list_all(tenant_id)

    return build_autonomy(
        tenant_id=tenant_id,
        actor_id=f"user:{user.user_id}",
        actor_species="human",
        permissions=user.permissions,
        budgets=budgets,
        stop_switch=stop_switch,
        outcomes=outcomes,
        safety_rows=safety_rows,
        sites=sites,
        learned_signals=learned,
        approval_policies=policies,
        site_id=site_id,
        action_type=action_type,
    )
