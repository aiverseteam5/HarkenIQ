"""Operational Agents API: the product noun, under the existing governance.

A0+A1 (2026-08-30). Create, scope, bind, activate and inspect named
Operational Agents, and read the proposals they produce.

Governance
----------
Reads need `fleet.view`; every mutation needs `site.manage` — the
ratified interim per the capability registry (design doc §3), with
`agent.manage` arriving at A2 after the A13.4 permission-matrix review.
No new permission is introduced here and none is broadened.

**Nothing on this router executes anything.** An agent's proposal is
decided on the SAME `/api/approvals/*` surface a human's action is, under
the same `action.approve`, and executes through the same node funnel.
There is no agent endpoint that reaches a device, and adding one would
be the parallel-governance shape the architecture forbids.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_cc.api.deps import get_session, require_permission
from harkeniq_cc.auth import UserContext
from harkeniq_cc.autonomy import LADDER, action_risk_map
from harkeniq_cc.db.repos import (
    AgentProposalRepo,
    AuditRepo,
    FleetCacheRepo,
    OperationalAgentRepo,
    SiteRepo,
)
from harkeniq_cc.governance import load_autonomy_contract
from harkeniq_cc.operational_agent import (
    AGENT_STATUSES,
    CAPABILITY_KINDS,
    KIND_ACTION_CLASS,
    KIND_READ,
    KIND_SKILL,
    READ_CAPABILITIES,
    REMEDIATION_CANDIDATES,
    REQUIRED_READS,
    SCOPE_DEVICE,
    SCOPE_DEVICE_CLASS,
    SCOPE_SITE,
    SCOPE_TYPES,
    STATUS_ACTIVE,
    STATUS_DRAFT,
    STATUS_PAUSED,
    STATUS_RETIRED,
    UNREACHABLE_CANDIDATE,
    agent_view,
    attribution_key,
)

logger = logging.getLogger("harkeniq.cc.api.operational_agents")

router = APIRouter(prefix="/api/operational-agents", tags=["operational-agents"])


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class ScopeRule(BaseModel):
    scope_type: str = Field(..., description="site | device_class | device")
    scope_ref: str = Field(..., min_length=1, max_length=128)


class CapabilityBinding(BaseModel):
    kind: str = Field(..., description="read | action_class | skill")
    capability_ref: str = Field(..., min_length=1, max_length=128)


class CreateAgentBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    description: str = Field("", max_length=512)
    autonomy_ceiling: int = Field(0, ge=0, le=3)
    require_approval_always: bool = True
    max_proposals_per_day: int = Field(25, ge=1, le=500)
    scopes: list[ScopeRule] = Field(default_factory=list)
    capabilities: list[CapabilityBinding] = Field(default_factory=list)


class UpdateAgentBody(BaseModel):
    description: Optional[str] = Field(None, max_length=512)
    autonomy_ceiling: Optional[int] = Field(None, ge=0, le=3)
    require_approval_always: Optional[bool] = None
    max_proposals_per_day: Optional[int] = Field(None, ge=1, le=500)


class BindingsBody(BaseModel):
    """Full replacement of scope and capability bindings.

    Replacement rather than patch: an operator reasoning about what an
    agent can reach should see the complete set in one request, not
    reconstruct it from a history of deltas.
    """

    scopes: list[ScopeRule]
    capabilities: list[CapabilityBinding]


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


def _agent_dict(agent, scopes=(), capabilities=()) -> dict:
    return {
        "id": agent.id,
        "name": agent.name,
        "description": agent.description,
        "status": agent.status,
        "version": agent.version,
        "actor": attribution_key(agent.id, agent.version),
        "species": "agent",
        "tenant_id": agent.tenant_id,
        "autonomy_ceiling": agent.autonomy_ceiling,
        "require_approval_always": agent.require_approval_always,
        "max_proposals_per_day": agent.max_proposals_per_day,
        "created_by": agent.created_by,
        "created_at": agent.created_at.isoformat() if agent.created_at else None,
        "updated_at": agent.updated_at.isoformat() if agent.updated_at else None,
        "activated_by": agent.activated_by,
        "activated_at": (
            agent.activated_at.isoformat() if agent.activated_at else None
        ),
        "last_evaluated_at": (
            agent.last_evaluated_at.isoformat() if agent.last_evaluated_at else None
        ),
        "scopes": [
            {"scope_type": s.scope_type, "scope_ref": s.scope_ref} for s in scopes
        ],
        "capabilities": [
            {"kind": c.kind, "capability_ref": c.capability_ref}
            for c in capabilities
        ],
    }


def proposal_dict(p) -> dict:
    """One proposal, with everything a decision-maker needs in the row.

    Shared with the approvals surface: an agent item in the queue is this
    payload plus the queue's own envelope, so the two can never describe
    the same proposal differently.
    """
    return {
        "proposal_id": p.id,
        "agent_id": p.agent_id,
        "actor": p.actor,
        "agent_version": p.agent_version,
        "site_id": p.site_id,
        "device_agent_id": p.device_agent_id,
        "action_type": p.action_type,
        "params": p.params or {},
        "rationale": p.rationale,
        "evidence": p.evidence or {},
        "disposition": p.disposition,
        "disposition_reason": p.disposition_reason,
        "blocking_conditions": p.blocking_conditions or [],
        "authorization_basis": p.authorization_basis,
        "status": p.status,
        "decided_by": p.decided_by,
        "decided_at": p.decided_at.isoformat() if p.decided_at else None,
        "directive_id": p.directive_id,
        "dispatch_reason": p.dispatch_reason,
        "dispatched_at": p.dispatched_at.isoformat() if p.dispatched_at else None,
        "outcome": p.outcome,
        "outcome_at": p.outcome_at.isoformat() if p.outcome_at else None,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


async def _validate_scopes(
    session: AsyncSession, tenant_id: str, scopes: list[ScopeRule]
) -> None:
    """Every scope reference must name something the tenant actually owns.

    A scope row pointing at another tenant's site, or at a device class
    the platform does not have, would be a silently empty agent at best
    and a cross-tenant reach at worst. Refuse it at write time.
    """
    site_ids = {s.id for s in await SiteRepo(session).list_all(tenant_id)}
    device_ids = {
        d.agent_id for d in await FleetCacheRepo(session).list_all(tenant_id)
    }
    for rule in scopes:
        if rule.scope_type not in SCOPE_TYPES:
            raise HTTPException(
                400, f"scope_type must be one of {list(SCOPE_TYPES)}"
            )
        if rule.scope_type == SCOPE_SITE and rule.scope_ref not in site_ids:
            raise HTTPException(
                400, f"site {rule.scope_ref!r} is not registered to this tenant"
            )
        if rule.scope_type == SCOPE_DEVICE_CLASS and rule.scope_ref.lower() not in (
            "server", "switch",
        ):
            raise HTTPException(
                400, "device_class must be 'server' or 'switch'"
            )
        if rule.scope_type == SCOPE_DEVICE and rule.scope_ref not in device_ids:
            # Not a hard tenancy hole (the fleet cache is tenant-scoped
            # already) but binding an agent to a device this tenant has
            # never seen is always a mistake worth surfacing now.
            raise HTTPException(
                400, f"device {rule.scope_ref!r} is not in this tenant's fleet"
            )


def _validate_capabilities(capabilities: list[CapabilityBinding]) -> None:
    """Bindings may only reference capabilities that already exist.

    This is the boundary that keeps "no agent-specific capabilities"
    true: an action class must be one the executor implements, and a
    read must be a governed CC surface. A typo here would otherwise
    create a capability that exists only inside an agent's bundle.
    """
    known_actions = set(action_risk_map())
    for binding in capabilities:
        if binding.kind not in CAPABILITY_KINDS:
            raise HTTPException(400, f"kind must be one of {list(CAPABILITY_KINDS)}")
        if binding.kind == KIND_ACTION_CLASS:
            if binding.capability_ref.upper() not in known_actions:
                raise HTTPException(
                    400,
                    f"{binding.capability_ref!r} is not an action class this "
                    f"platform can execute",
                )
        elif binding.kind == KIND_READ:
            if binding.capability_ref.lower() not in READ_CAPABILITIES:
                raise HTTPException(
                    400,
                    f"{binding.capability_ref!r} is not a governed read "
                    f"capability ({', '.join(sorted(READ_CAPABILITIES))})",
                )


async def _apply_bindings(
    session: AsyncSession,
    repo: OperationalAgentRepo,
    agent,
    scopes: list[ScopeRule],
    capabilities: list[CapabilityBinding],
) -> None:
    await repo.clear_scopes(agent.id)
    await repo.clear_capabilities(agent.id)
    await session.flush()
    seen_scope: set[tuple[str, str]] = set()
    for rule in scopes:
        key = (rule.scope_type, rule.scope_ref)
        if key in seen_scope:
            continue
        seen_scope.add(key)
        await repo.add_scope(
            agent_id=agent.id,
            tenant_id=agent.tenant_id,
            scope_type=rule.scope_type,
            scope_ref=rule.scope_ref,
        )
    seen_cap: set[tuple[str, str]] = set()
    bindings = list(capabilities)
    # An agent must be able to observe the condition it proposes against
    # and read the contract it claims authority from. Adding these is not
    # a grant: each read has its own guard on its own route.
    for required in REQUIRED_READS:
        if not any(
            b.kind == KIND_READ and b.capability_ref.lower() == required
            for b in bindings
        ):
            bindings.append(CapabilityBinding(kind=KIND_READ, capability_ref=required))
    for binding in bindings:
        ref = (
            binding.capability_ref.upper()
            if binding.kind == KIND_ACTION_CLASS
            else binding.capability_ref.lower()
            if binding.kind == KIND_READ
            else binding.capability_ref
        )
        key = (binding.kind, ref)
        if key in seen_cap:
            continue
        seen_cap.add(key)
        await repo.add_capability(
            agent_id=agent.id,
            tenant_id=agent.tenant_id,
            kind=binding.kind,
            capability_ref=ref,
        )


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------


@router.get(
    "/catalogue",
    dependencies=[Depends(require_permission("fleet.view"))],
)
async def catalogue(
    user: UserContext = Depends(require_permission("fleet.view")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """What an agent CAN be bound to in this tenant, and what each means.

    The building blocks, read from the platform itself rather than from a
    hand-kept list: action classes come from the executor's own risk
    classification, sites from the tenant's registry, reads from the
    governed CC surfaces. This is the shape a natural-language agent
    builder (A7) will later compile INTO, which is why it is a contract
    rather than a form's option list.
    """
    risks = action_risk_map()
    sites = await SiteRepo(session).list_all(user.tenant_id)
    devices = await FleetCacheRepo(session).list_all(user.tenant_id)

    # Which conditions the platform knows a remediation for. Stated so an
    # operator can see WHY binding a class to an agent would ever fire.
    triggers: dict[str, list[str]] = {}
    for subsystem, candidates in REMEDIATION_CANDIDATES.items():
        for cand in candidates:
            triggers.setdefault(cand["action_type"], []).append(subsystem)
    triggers.setdefault(UNREACHABLE_CANDIDATE["action_type"], []).append(
        "unreachable management controller"
    )

    return {
        "action_classes": [
            {
                "action_type": at,
                "risk": risk,
                "proposable": at in triggers,
                "observed_conditions": sorted(triggers.get(at, [])),
                "note": (
                    "The platform has no observed condition mapped to this "
                    "class, so an agent bound to it would never propose it."
                    if at not in triggers else ""
                ),
            }
            for at, risk in sorted(risks.items())
        ],
        "read_capabilities": [
            {
                "ref": ref,
                "description": desc,
                "required": ref in REQUIRED_READS,
            }
            for ref, desc in sorted(READ_CAPABILITIES.items())
        ],
        "scope_options": {
            "sites": [{"id": s.id, "name": s.site_name} for s in sites],
            "device_classes": sorted(
                {(d.device_class or "server") for d in devices}
            ) or ["server"],
            "device_count": len(devices),
        },
        "ladder": LADDER,
    }


# ---------------------------------------------------------------------------
# CRUD + lifecycle
# ---------------------------------------------------------------------------


@router.get(
    "/",
    dependencies=[Depends(require_permission("fleet.view"))],
)
async def list_agents(
    status: str | None = Query(None, description="draft|active|paused|retired"),
    user: UserContext = Depends(require_permission("fleet.view")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    repo = OperationalAgentRepo(session)
    agents = await repo.list_all(user.tenant_id, status=status)
    prop_repo = AgentProposalRepo(session)
    items = []
    for agent in agents:
        scopes = await repo.list_scopes(agent.id)
        caps = await repo.list_capabilities(agent.id)
        proposals = await prop_repo.list_for_agent(user.tenant_id, agent.id)
        row = _agent_dict(agent, scopes, caps)
        row["proposal_counts"] = {
            status_name: sum(1 for p in proposals if p.status == status_name)
            for status_name in sorted({p.status for p in proposals})
        }
        items.append(row)
    return {"agents": items, "total": len(items), "tenant_id": user.tenant_id}


@router.post(
    "/",
    status_code=201,
    dependencies=[Depends(require_permission("site.manage"))],
)
async def create_agent(
    body: CreateAgentBody,
    user: UserContext = Depends(require_permission("site.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Create an agent. It starts in `draft` and evaluates nothing.

    Activation is a separate, human, audited act: creating a bundle and
    turning it loose must never be the same request.
    """
    repo = OperationalAgentRepo(session)
    if await repo.get_by_name(user.tenant_id, body.name) is not None:
        raise HTTPException(409, f"an agent named {body.name!r} already exists")
    await _validate_scopes(session, user.tenant_id, body.scopes)
    _validate_capabilities(body.capabilities)

    actor = user.email or user.user_id
    agent = await repo.create(
        tenant_id=user.tenant_id,
        name=body.name,
        description=body.description,
        autonomy_ceiling=body.autonomy_ceiling,
        require_approval_always=body.require_approval_always,
        max_proposals_per_day=body.max_proposals_per_day,
        created_by=actor,
    )
    await _apply_bindings(session, repo, agent, body.scopes, body.capabilities)
    await AuditRepo(session).append(
        actor=actor,
        action="operational_agent.created",
        subject=agent.id,
        tenant_id=user.tenant_id,
        detail={
            "name": agent.name,
            "autonomy_ceiling": agent.autonomy_ceiling,
            "require_approval_always": agent.require_approval_always,
            "scopes": [[s.scope_type, s.scope_ref] for s in body.scopes],
            "capabilities": [[c.kind, c.capability_ref] for c in body.capabilities],
        },
    )
    await session.commit()
    return _agent_dict(
        agent,
        await repo.list_scopes(agent.id),
        await repo.list_capabilities(agent.id),
    )


async def _require_agent(session: AsyncSession, tenant_id: str, agent_id: str):
    agent = await OperationalAgentRepo(session).get(tenant_id, agent_id)
    if agent is None:
        raise HTTPException(404, "operational agent not found")
    return agent


@router.get(
    "/{agent_id}",
    dependencies=[Depends(require_permission("fleet.view"))],
)
async def get_agent(
    agent_id: str,
    user: UserContext = Depends(require_permission("fleet.view")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """One agent, answered the way an operator asks about it.

    What is it, what can it see, what can it do, what may it do without
    me, why, what needs my approval, what did it do, what happened. Every
    answer composes from the same governed contracts the Console and a
    future MCP caller read.
    """
    agent = await _require_agent(session, user.tenant_id, agent_id)
    repo = OperationalAgentRepo(session)
    scopes = await repo.list_scopes(agent.id)
    caps = await repo.list_capabilities(agent.id)
    devices = await FleetCacheRepo(session).list_all(user.tenant_id)
    contract = await load_autonomy_contract(
        session,
        tenant_id=user.tenant_id,
        actor_id=attribution_key(agent.id, agent.version),
        actor_species="agent",
        permissions=user.permissions,
    )
    proposals = await AgentProposalRepo(session).list_for_agent(
        user.tenant_id, agent.id,
    )
    view = agent_view(
        agent=agent,
        scopes=scopes,
        capabilities=caps,
        devices=devices,
        autonomy_contract=contract,
        proposals=proposals,
    )
    view["proposals"] = [proposal_dict(p) for p in proposals[:50]]
    view["posture"] = {
        "tenant_level": contract["posture"]["configured_level"],
        "stop_switch": contract["posture"]["stop_switch"],
        "safety_reported": contract["safety_state"]["reported"],
        "sites_not_reporting": contract["safety_state"]["sites_not_reporting"],
    }
    return view


@router.patch(
    "/{agent_id}",
    dependencies=[Depends(require_permission("site.manage"))],
)
async def update_agent(
    agent_id: str,
    body: UpdateAgentBody,
    user: UserContext = Depends(require_permission("site.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    agent = await _require_agent(session, user.tenant_id, agent_id)
    if agent.status == STATUS_RETIRED:
        raise HTTPException(409, "a retired agent cannot be reconfigured")
    repo = OperationalAgentRepo(session)
    actor = user.email or user.user_id
    changed: dict = {}
    if body.description is not None:
        agent.description, changed["description"] = body.description, body.description
    if body.autonomy_ceiling is not None:
        agent.autonomy_ceiling = body.autonomy_ceiling
        changed["autonomy_ceiling"] = body.autonomy_ceiling
    if body.require_approval_always is not None:
        agent.require_approval_always = body.require_approval_always
        changed["require_approval_always"] = body.require_approval_always
    if body.max_proposals_per_day is not None:
        agent.max_proposals_per_day = body.max_proposals_per_day
        changed["max_proposals_per_day"] = body.max_proposals_per_day
    if not changed:
        raise HTTPException(400, "no fields to update")
    await repo.bump_version(agent, actor)
    await AuditRepo(session).append(
        actor=actor,
        action="operational_agent.updated",
        subject=agent.id,
        tenant_id=user.tenant_id,
        detail={"changed": changed, "version": agent.version},
    )
    await session.commit()
    return _agent_dict(
        agent,
        await repo.list_scopes(agent.id),
        await repo.list_capabilities(agent.id),
    )


@router.put(
    "/{agent_id}/bindings",
    dependencies=[Depends(require_permission("site.manage"))],
)
async def replace_bindings(
    agent_id: str,
    body: BindingsBody,
    user: UserContext = Depends(require_permission("site.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Replace what this agent can see and what it can propose."""
    agent = await _require_agent(session, user.tenant_id, agent_id)
    if agent.status == STATUS_RETIRED:
        raise HTTPException(409, "a retired agent cannot be reconfigured")
    await _validate_scopes(session, user.tenant_id, body.scopes)
    _validate_capabilities(body.capabilities)
    repo = OperationalAgentRepo(session)
    actor = user.email or user.user_id
    await _apply_bindings(session, repo, agent, body.scopes, body.capabilities)
    await repo.bump_version(agent, actor)
    await AuditRepo(session).append(
        actor=actor,
        action="operational_agent.bound",
        subject=agent.id,
        tenant_id=user.tenant_id,
        detail={
            "version": agent.version,
            "scopes": [[s.scope_type, s.scope_ref] for s in body.scopes],
            "capabilities": [[c.kind, c.capability_ref] for c in body.capabilities],
        },
    )
    await session.commit()
    return _agent_dict(
        agent,
        await repo.list_scopes(agent.id),
        await repo.list_capabilities(agent.id),
    )


_TRANSITIONS = {
    "activate": (STATUS_ACTIVE, (STATUS_DRAFT, STATUS_PAUSED)),
    "pause": (STATUS_PAUSED, (STATUS_ACTIVE,)),
    "retire": (STATUS_RETIRED, (STATUS_DRAFT, STATUS_ACTIVE, STATUS_PAUSED)),
}


@router.post(
    "/{agent_id}/{transition}",
    dependencies=[Depends(require_permission("site.manage"))],
)
async def transition_agent(
    agent_id: str,
    transition: str,
    user: UserContext = Depends(require_permission("site.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Activate, pause or retire. Always a human act, always audited.

    Activation refuses an agent that could not do anything: an agent with
    no scope sees nothing and an agent with no action class proposes
    nothing, and activating one would be a switch that reports success
    while changing nothing.
    """
    if transition not in _TRANSITIONS:
        raise HTTPException(404, "unknown transition")
    target, allowed_from = _TRANSITIONS[transition]
    agent = await _require_agent(session, user.tenant_id, agent_id)
    if agent.status not in allowed_from:
        raise HTTPException(
            409,
            f"cannot {transition} an agent in status {agent.status!r}",
        )
    repo = OperationalAgentRepo(session)
    if target == STATUS_ACTIVE:
        scopes = await repo.list_scopes(agent.id)
        caps = await repo.list_capabilities(agent.id)
        if not scopes:
            raise HTTPException(
                409, "cannot activate an agent with no scope: it would see nothing"
            )
        if not any(c.kind == KIND_ACTION_CLASS for c in caps):
            raise HTTPException(
                409,
                "cannot activate an agent with no action class bound: it would "
                "propose nothing",
            )
    actor = user.email or user.user_id
    await repo.set_status(agent, target, actor)
    await AuditRepo(session).append(
        actor=actor,
        action=f"operational_agent.{transition}d",
        subject=agent.id,
        tenant_id=user.tenant_id,
        detail={"status": target, "version": agent.version},
    )
    await session.commit()
    return _agent_dict(
        agent,
        await repo.list_scopes(agent.id),
        await repo.list_capabilities(agent.id),
    )


@router.get(
    "/{agent_id}/proposals",
    dependencies=[Depends(require_permission("fleet.view"))],
)
async def list_proposals(
    agent_id: str,
    limit: int = Query(100, ge=1, le=500),
    user: UserContext = Depends(require_permission("fleet.view")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Every proposal this agent has made, including the blocked ones.

    Blocked proposals are the point: an agent that wanted to act and was
    refused is exactly what an operator needs to see before raising a
    level, and hiding them would make the governance invisible.
    """
    await _require_agent(session, user.tenant_id, agent_id)
    proposals = await AgentProposalRepo(session).list_for_agent(
        user.tenant_id, agent_id, limit=limit,
    )
    return {
        "proposals": [proposal_dict(p) for p in proposals],
        "total": len(proposals),
        "agent_id": agent_id,
        "tenant_id": user.tenant_id,
    }
