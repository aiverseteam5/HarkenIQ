"""Policies API: approval policies, approval groups, and autonomy budgets."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_cc.api.deps import get_cc_state, get_session, require_permission
from harkeniq_cc.approval_policy import MODE_AUTO_APPROVE
from harkeniq_cc.auth import UserContext
from harkeniq_cc.db.repos import (
    ApprovalGroupRepo,
    ApprovalPolicyRepo,
    AuditRepo,
    AutonomyBudgetRepo,
    StopSwitchRepo,
)

router = APIRouter(prefix="/api/policies", tags=["policies"])


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------


class PolicyCreateRequest(BaseModel):
    name: str
    device_type: str = "*"
    action_type: str = "*"
    # E0.1: was "medium", while the other two selectors defaulted to "*".
    # A policy created as "dual approval for everything" therefore
    # governed medium-risk actions ONLY, silently. Found on the live
    # stack: a dual policy for COLLECT_DIAGNOSTICS (risk "none") matched
    # nothing. All three selectors now default to the wildcard.
    risk_level: str = "*"
    time_window_json: Optional[dict] = None
    approval_mode: str = "require_approval"
    required_approvers: int = 1
    group_id: Optional[str] = None


class PolicyUpdateRequest(BaseModel):
    name: Optional[str] = None
    device_type: Optional[str] = None
    action_type: Optional[str] = None
    risk_level: Optional[str] = None
    time_window_json: Optional[dict] = None
    approval_mode: Optional[str] = None
    required_approvers: Optional[int] = None
    group_id: Optional[str] = None
    status: Optional[str] = None


class GroupCreateRequest(BaseModel):
    name: str
    slack_channel: str = ""
    github_team: str = ""
    required_count: int = 1
    escalation_chain: Optional[dict] = None


class GroupUpdateRequest(BaseModel):
    name: Optional[str] = None
    slack_channel: Optional[str] = None
    github_team: Optional[str] = None
    required_count: Optional[int] = None
    escalation_chain: Optional[dict] = None


class GroupMemberRequest(BaseModel):
    email: str
    role: str = "approver"
    #: Keycloak subject. Optional: a membership added by email alone
    #: still matches, it just cannot survive an address change.
    principal_ref: str = ""


class BudgetCreateRequest(BaseModel):
    device_type: str = "*"
    level: int = 0
    budget_limit: int = 0
    budget_period: str = "monthly"
    learning_ramp_config: Optional[dict] = None


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------


def _policy_dict(p) -> dict:
    return {
        "id": p.id,
        "tenant_id": p.tenant_id,
        "name": p.name,
        "device_type": p.device_type,
        "action_type": p.action_type,
        "risk_level": p.risk_level,
        "time_window_json": p.time_window_json,
        "approval_mode": p.approval_mode,
        "required_approvers": p.required_approvers,
        "group_id": p.group_id,
        "status": p.status,
        "created_by": p.created_by,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


def _group_dict(g) -> dict:
    return {
        "id": g.id,
        "tenant_id": g.tenant_id,
        "name": g.name,
        "slack_channel": g.slack_channel,
        "github_team": g.github_team,
        "required_count": g.required_count,
        "escalation_chain": g.escalation_chain,
        "created_by": g.created_by,
        "created_at": g.created_at.isoformat() if g.created_at else None,
    }


def _budget_dict(b) -> dict:
    return {
        "id": b.id,
        "tenant_id": b.tenant_id,
        "device_type": b.device_type,
        "level": b.level,
        "budget_limit": b.budget_limit,
        "budget_period": b.budget_period,
        "actions_used": b.actions_used,
        "learning_ramp_config": b.learning_ramp_config,
        "created_at": b.created_at.isoformat() if b.created_at else None,
        "updated_at": b.updated_at.isoformat() if b.updated_at else None,
    }


# ---------------------------------------------------------------------------
# Approval Policies
# ---------------------------------------------------------------------------


@router.get(
    "/",
    # A13/E0.3: knowing that an action needs two approvers is POSTURE,
    # and the D2 read-split already made posture readable to the people
    # living under it (list_autonomy_budgets below says the same). The
    # auditor's read-only-everything scope depends on this. Every
    # mutation stays at site.manage.
    dependencies=[Depends(require_permission("fleet.view"))],
)
async def list_policies(
    user: UserContext = Depends(require_permission("fleet.view")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """List approval policies for the tenant."""
    policies = await ApprovalPolicyRepo(session).list_all(user.tenant_id)
    return {
        "policies": [_policy_dict(p) for p in policies],
        "total": len(policies),
        "tenant_id": user.tenant_id,
    }


def _reject_auto_approve(mode: Optional[str]) -> None:
    """E0.1: `auto_approve` is not a policy this platform will store.

    While approval policies were unenforced this mode was inert. Enforcing
    it as written would make one policy row a second, ungoverned path to
    unattended execution: no evidence bar, no budget, no error-budget
    drop-back, and no fence for the risk-`high` classes that
    `never_budget_grantable` refuses at EVERY autonomy level.

    The tenant's autonomy contract is the one governed answer to "may
    this run without a human". Raising the autonomy level is how a class
    earns that, and only a human can do it.
    """
    if mode is not None and mode.lower() == MODE_AUTO_APPROVE:
        raise HTTPException(
            status_code=400,
            detail=(
                "approval_mode 'auto_approve' is refused: unattended "
                "execution is granted by the autonomy contract, which "
                "requires evidence and a human decision, not by an "
                "approval policy. Raise the tenant's autonomy level for "
                "this action class instead."
            ),
        )


@router.post(
    "/",
    dependencies=[Depends(require_permission("site.manage"))],
)
async def create_policy(
    body: PolicyCreateRequest,
    user: UserContext = Depends(require_permission("site.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Create an approval policy."""
    _reject_auto_approve(body.approval_mode)
    policy = await ApprovalPolicyRepo(session).create(
        tenant_id=user.tenant_id,
        name=body.name,
        created_by=user.user_id,
        device_type=body.device_type,
        action_type=body.action_type,
        risk_level=body.risk_level,
        time_window_json=body.time_window_json,
        approval_mode=body.approval_mode,
        required_approvers=body.required_approvers,
        group_id=body.group_id,
    )
    await AuditRepo(session).append(
        actor=user.user_id,
        action="policy.create",
        subject=policy.id,
        tenant_id=user.tenant_id,
        detail={"name": body.name},
    )
    await session.commit()
    return {"policy": _policy_dict(policy)}


@router.patch(
    "/{policy_id}",
    dependencies=[Depends(require_permission("site.manage"))],
)
async def update_policy(
    policy_id: str,
    body: PolicyUpdateRequest,
    user: UserContext = Depends(require_permission("site.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Update an approval policy."""
    _reject_auto_approve(body.approval_mode)
    repo = ApprovalPolicyRepo(session)
    policy = await repo.get_by_id(policy_id)
    if policy is None or policy.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="policy not found")

    updates = body.model_dump(exclude_none=True)
    await repo.update(policy, **updates)
    await AuditRepo(session).append(
        actor=user.user_id,
        action="policy.update",
        subject=policy_id,
        tenant_id=user.tenant_id,
        detail=updates,
    )
    await session.commit()
    return {"policy": _policy_dict(policy)}


@router.delete(
    "/{policy_id}",
    dependencies=[Depends(require_permission("site.manage"))],
)
async def delete_policy(
    policy_id: str,
    user: UserContext = Depends(require_permission("site.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Delete an approval policy."""
    repo = ApprovalPolicyRepo(session)
    policy = await repo.get_by_id(policy_id)
    if policy is None or policy.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="policy not found")
    await repo.delete(policy)
    await AuditRepo(session).append(
        actor=user.user_id,
        action="policy.delete",
        subject=policy_id,
        tenant_id=user.tenant_id,
    )
    await session.commit()
    return {"deleted": True, "policy_id": policy_id}


# ---------------------------------------------------------------------------
# Approval Groups
# ---------------------------------------------------------------------------


@router.get(
    "/groups",
    # A13/E0.3: who may approve is posture too. Read-split as above.
    dependencies=[Depends(require_permission("fleet.view"))],
)
async def list_groups(
    user: UserContext = Depends(require_permission("fleet.view")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """List approval groups for the tenant."""
    repo = ApprovalGroupRepo(session)
    groups = await repo.list_all(user.tenant_id)
    rows = []
    for g in groups:
        entry = _group_dict(g)
        entry["members_count"] = len(await repo.list_members(g.id))
        rows.append(entry)
    return {
        "groups": rows,
        "total": len(groups),
        "tenant_id": user.tenant_id,
    }


@router.post(
    "/groups",
    dependencies=[Depends(require_permission("site.manage"))],
)
async def create_group(
    body: GroupCreateRequest,
    user: UserContext = Depends(require_permission("site.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Create an approval group."""
    group = await ApprovalGroupRepo(session).create(
        tenant_id=user.tenant_id,
        name=body.name,
        created_by=user.user_id,
        slack_channel=body.slack_channel,
        github_team=body.github_team,
        required_count=body.required_count,
        escalation_chain=body.escalation_chain,
    )
    await AuditRepo(session).append(
        actor=user.user_id,
        action="group.create",
        subject=group.id,
        tenant_id=user.tenant_id,
        detail={"name": body.name},
    )
    await session.commit()
    return {"group": _group_dict(group)}


@router.get(
    "/groups/{group_id}",
    # A13/E0.3: same read-split as the listing above.
    dependencies=[Depends(require_permission("fleet.view"))],
)
async def get_group(
    group_id: str,
    user: UserContext = Depends(require_permission("fleet.view")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Group detail with members (QA-036: the Console detail panel's shape)."""
    repo = ApprovalGroupRepo(session)
    group = await repo.get_by_id(group_id)
    if group is None or group.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="group not found")
    members = await repo.list_members(group_id)
    payload = _group_dict(group)
    payload["members"] = [
        {
            "id": m.id, "email": m.user_email, "role": m.role,
            "principal_ref": m.principal_ref or "",
            "subject_bound": bool(m.principal_ref),
        }
        for m in members
    ]
    payload["members_count"] = len(members)
    return payload


@router.post(
    "/groups/{group_id}/members",
    dependencies=[Depends(require_permission("site.manage"))],
)
async def add_group_member(
    group_id: str,
    body: GroupMemberRequest,
    user: UserContext = Depends(require_permission("site.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Add a member to an approval group (QA-036: route existed only in the UI)."""
    repo = ApprovalGroupRepo(session)
    group = await repo.get_by_id(group_id)
    if group is None or group.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="group not found")
    member = await repo.add_member(
        group_id, body.email, body.role,
        principal_ref=getattr(body, "principal_ref", "") or "",
    )
    await AuditRepo(session).append(
        actor=user.user_id,
        action="group.member.add",
        subject=group_id,
        tenant_id=user.tenant_id,
        detail={"email": body.email, "role": body.role},
    )
    await session.commit()
    return {"member": {"id": member.id, "email": member.user_email,
                       "role": member.role}}


@router.delete(
    "/groups/{group_id}/members/{member_id}",
    dependencies=[Depends(require_permission("site.manage"))],
)
async def remove_group_member(
    group_id: str,
    member_id: str,
    user: UserContext = Depends(require_permission("site.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Remove a member from an approval group."""
    repo = ApprovalGroupRepo(session)
    group = await repo.get_by_id(group_id)
    if group is None or group.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="group not found")
    member = await repo.get_member(member_id)
    if member is None or member.group_id != group_id:
        raise HTTPException(status_code=404, detail="member not found")
    await repo.remove_member(member)
    await AuditRepo(session).append(
        actor=user.user_id,
        action="group.member.remove",
        subject=group_id,
        tenant_id=user.tenant_id,
        detail={"email": member.user_email},
    )
    await session.commit()
    return {"removed": True, "member_id": member_id}


@router.patch(
    "/groups/{group_id}",
    dependencies=[Depends(require_permission("site.manage"))],
)
async def update_group(
    group_id: str,
    body: GroupUpdateRequest,
    user: UserContext = Depends(require_permission("site.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Update an approval group."""
    repo = ApprovalGroupRepo(session)
    group = await repo.get_by_id(group_id)
    if group is None or group.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="group not found")

    updates = body.model_dump(exclude_none=True)
    await repo.update(group, **updates)
    await AuditRepo(session).append(
        actor=user.user_id,
        action="group.update",
        subject=group_id,
        tenant_id=user.tenant_id,
        detail=updates,
    )
    await session.commit()
    return {"group": _group_dict(group)}


@router.delete(
    "/groups/{group_id}",
    dependencies=[Depends(require_permission("site.manage"))],
)
async def delete_group(
    group_id: str,
    user: UserContext = Depends(require_permission("site.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Delete an approval group (and its members)."""
    repo = ApprovalGroupRepo(session)
    group = await repo.get_by_id(group_id)
    if group is None or group.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="group not found")
    await repo.delete(group)
    await AuditRepo(session).append(
        actor=user.user_id,
        action="group.delete",
        subject=group_id,
        tenant_id=user.tenant_id,
    )
    await session.commit()
    return {"deleted": True, "group_id": group_id}


# ---------------------------------------------------------------------------
# Autonomy Budgets
# ---------------------------------------------------------------------------


@router.get(
    "/autonomy",
    # S1 2026-08-29 (decision D2): POSTURE IS READABLE BY EVERY TENANT
    # ROLE. The trust ladder must be visible to the people living under
    # it — operators and viewers see what the system may do autonomously;
    # only site.manage may change it (the POST/DELETE below are
    # unchanged, and D2 forbids broadening mutation permissions).
    dependencies=[Depends(require_permission("fleet.view"))],
)
async def list_autonomy_budgets(
    user: UserContext = Depends(require_permission("fleet.view")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """List autonomy budgets for the tenant."""
    budgets = await AutonomyBudgetRepo(session).list_all(user.tenant_id)
    return {
        "budgets": [_budget_dict(b) for b in budgets],
        "total": len(budgets),
        "tenant_id": user.tenant_id,
    }


@router.post(
    "/autonomy",
    dependencies=[Depends(require_permission("site.manage"))],
)
async def create_autonomy_budget(
    body: BudgetCreateRequest,
    user: UserContext = Depends(require_permission("site.manage")),
    session: AsyncSession = Depends(get_session),
    state=Depends(get_cc_state),
) -> dict:
    """Create or update an autonomy budget (upserts on tenant_id + device_type)."""
    budget = await AutonomyBudgetRepo(session).upsert(
        tenant_id=user.tenant_id,
        device_type=body.device_type,
        level=body.level,
        budget_limit=body.budget_limit,
        budget_period=body.budget_period,
        learning_ramp_config=body.learning_ramp_config,
    )
    await AuditRepo(session).append(
        actor=user.user_id,
        action="autonomy.upsert",
        subject=budget.id,
        tenant_id=user.tenant_id,
        detail={"device_type": body.device_type, "level": body.level},
    )
    await session.commit()
    # QA-022: budgets shape leases; propagate to SMs now
    from harkeniq_cc.policy_push import push_policy_to_all_sites
    await push_policy_to_all_sites(state.config, state.sessionmaker)
    return {"budget": _budget_dict(budget)}


@router.delete(
    "/autonomy/{budget_id}",
    dependencies=[Depends(require_permission("site.manage"))],
)
async def delete_autonomy_budget(
    budget_id: str,
    user: UserContext = Depends(require_permission("site.manage")),
    session: AsyncSession = Depends(get_session),
    state=Depends(get_cc_state),
) -> dict:
    """Delete an autonomy budget."""
    repo = AutonomyBudgetRepo(session)
    budget = await repo.get_by_id(budget_id)
    if budget is None or budget.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="budget not found")
    await repo.delete(budget)
    await AuditRepo(session).append(
        actor=user.user_id,
        action="autonomy.delete",
        subject=budget_id,
        tenant_id=user.tenant_id,
    )
    await session.commit()
    # QA-022: budgets shape leases; propagate to SMs now
    from harkeniq_cc.policy_push import push_policy_to_all_sites
    await push_policy_to_all_sites(state.config, state.sessionmaker)
    return {"deleted": True, "budget_id": budget_id}


# ---------------------------------------------------------------------------
# R3a: Stop Switch (A2.2)
# ---------------------------------------------------------------------------
# QA-022: persisted per-tenant (cc_stop_switch) and pushed to every SM
# immediately on a flip, then re-converged by the fleet-poll cycle.


async def _flip_stop_switch(active: bool, user, session, state) -> dict:
    await StopSwitchRepo(session).set(
        user.tenant_id, active, changed_by=user.user_id
    )
    await AuditRepo(session).append(
        actor=user.user_id,
        action="stop_switch.activate" if active else "stop_switch.deactivate",
        subject=user.tenant_id,
        tenant_id=user.tenant_id,
        detail={"changed_by": user.user_id},
    )
    await session.commit()
    # Propagate now — agents drop to observe-only on the next lease
    # renewal, not the next poll tick. Push failures are logged, never
    # surfaced: the persisted row re-converges via the fleet poller.
    from harkeniq_cc.policy_push import push_policy_to_all_sites

    pushed = await push_policy_to_all_sites(state.config, state.sessionmaker)
    return {
        "stop_switch": active,
        "tenant_id": user.tenant_id,
        "sites_pushed": pushed,
    }


@router.post(
    "/stop-switch",
    dependencies=[Depends(require_permission("site.manage"))],
)
async def activate_stop_switch(
    user: UserContext = Depends(require_permission("site.manage")),
    session: AsyncSession = Depends(get_session),
    state=Depends(get_cc_state),
) -> dict:
    """Activate the fleet-wide stop switch: deny all autonomous actions.

    This is an emergency mechanism (spec A2.2).  All agents will drop to
    observe-only once their current lease expires.  SM propagates the
    stop switch state via the next lease renewal.
    """
    return await _flip_stop_switch(True, user, session, state)


@router.post(
    "/stop-switch/deactivate",
    dependencies=[Depends(require_permission("site.manage"))],
)
async def deactivate_stop_switch(
    user: UserContext = Depends(require_permission("site.manage")),
    session: AsyncSession = Depends(get_session),
    state=Depends(get_cc_state),
) -> dict:
    """Deactivate the stop switch: resume normal autonomous operation."""
    return await _flip_stop_switch(False, user, session, state)


@router.get("/stop-switch")
async def get_stop_switch_state(
    user: UserContext = Depends(require_permission("fleet.view")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Get current stop switch state for this tenant."""
    active = await StopSwitchRepo(session).is_active(user.tenant_id)
    return {"stop_switch": active}
