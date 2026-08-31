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

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_cc.api.deps import forbid_out_of_scope, get_scope, get_session, require_permission
from harkeniq_cc.auth import UserContext
from harkeniq.capabilities import action_facts
from harkeniq_cc.agent_activation import activation_provenance
from harkeniq_cc.approval_policy import STATE_APPROVED
from harkeniq_cc.autonomy import LADDER, action_risk_map
from harkeniq_cc.capabilities import reachable_action_classes
from harkeniq_cc.db.repos import (
    AgentProposalRepo,
    AuditRepo,
    FleetCacheRepo,
    OrgUnitRepo,
    OperationalAgentRepo,
    SiteRepo,
)
from harkeniq_cc.governance import load_agent_scope, load_autonomy_contract
from harkeniq_cc.scope import SCOPE_DEVICE, SCOPE_ORG_UNIT, SCOPE_SITE
from harkeniq_cc.operational_agent import (
    AGENT_STATUSES,
    SCOPE_ORG_UNIT as AGENT_SCOPE_ORG_UNIT,
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
    resolve_scope,
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


#: A2/D2: the windows a per-agent execution budget may be measured over.
#: Kept small and explicit; an arbitrary duration would make two agents'
#: budgets incomparable in the same report.
BUDGET_PERIODS = ("daily", "weekly", "monthly")


class CreateAgentBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    description: str = Field("", max_length=512)
    autonomy_ceiling: int = Field(0, ge=0, le=3)
    require_approval_always: bool = True
    max_proposals_per_day: int = Field(25, ge=1, le=500)
    #: A2/D2. 0 means unset -- the tenant and site budgets still apply.
    #: This can only ever narrow them; it never grants execution.
    execution_budget: int = Field(0, ge=0, le=10000)
    budget_period: str = Field("daily")
    scopes: list[ScopeRule] = Field(default_factory=list)
    capabilities: list[CapabilityBinding] = Field(default_factory=list)


class UpdateAgentBody(BaseModel):
    description: Optional[str] = Field(None, max_length=512)
    autonomy_ceiling: Optional[int] = Field(None, ge=0, le=3)
    require_approval_always: Optional[bool] = None
    max_proposals_per_day: Optional[int] = Field(None, ge=1, le=500)
    execution_budget: Optional[int] = Field(None, ge=0, le=10000)
    budget_period: Optional[str] = None
    #: A2: per-agent pause. A non-empty reason stops unattended work; ""
    #: lifts it. It can only tighten -- lifting this cannot resume an
    #: agent a tenant or site stop switch has halted.
    paused_reason: Optional[str] = Field(None, max_length=512)


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
        # A19.9: the configuration actually switched on, reported by the
        # transition and the LIST too, not only by the detail read -- a
        # caller that just activated should not have to ask again to
        # learn what it activated, and an operator scanning the list
        # should see a drifted agent without opening it. From the one
        # provenance rule, so no view can disagree with another.
        **activation_provenance(agent),
        "execution_budget": int(agent.execution_budget or 0),
        "budget_period": agent.budget_period,
        "paused_reason": agent.paused_reason or None,
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
    unit_ids = {u.id for u in await OrgUnitRepo(session).list_all(tenant_id)}
    device_ids = {
        d.agent_id for d in await FleetCacheRepo(session).list_all(tenant_id)
    }
    for rule in scopes:
        if rule.scope_type not in SCOPE_TYPES:
            raise HTTPException(
                400, f"scope_type must be one of {list(SCOPE_TYPES)}"
            )
        if rule.scope_type == AGENT_SCOPE_ORG_UNIT and rule.scope_ref not in unit_ids:
            raise HTTPException(
                400,
                f"org unit {rule.scope_ref!r} does not exist in this tenant",
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


#: Platform capability truth, read from the protocols' own declarations.
#: A module-level read is correct here: which action classes have code
#: behind them is a property of the BUILD, not of a tenant, a request or
#: a device, and it cannot change while the process runs.
_PLATFORM_FACTS = action_facts()


def _validate_capabilities(capabilities: list[CapabilityBinding]) -> None:
    """Bindings may only reference capabilities that already exist.

    This is the boundary that keeps "no agent-specific capabilities"
    true: an action class must be one the executor implements, and a
    read must be a governed CC surface. A typo here would otherwise
    create a capability that exists only inside an agent's bundle.
    """
    known_actions = set(action_risk_map())
    for binding in capabilities:
        if binding.kind == KIND_SKILL:
            # A2: skill bindings are real now. E0.3 refused them rather
            # than leave them accepted and inert, and named the four
            # missing pieces; all four exist. A binding is accepted here
            # and JUDGED at preflight, where the Registry can be asked
            # whether the agent's own devices can perform what the skill
            # recommends -- a question that needs the agent's scope, so
            # it cannot be answered on this line.
            if not binding.capability_ref.strip():
                raise HTTPException(400, "a skill binding needs a skill id")
            continue
        if binding.kind not in CAPABILITY_KINDS:
            raise HTTPException(400, f"kind must be one of {list(CAPABILITY_KINDS)}")
        if binding.kind == KIND_ACTION_CLASS:
            ref = binding.capability_ref.upper()
            if ref not in known_actions:
                raise HTTPException(
                    400,
                    f"{binding.capability_ref!r} is not an action class this "
                    f"platform can execute",
                )
            # Capability Registry. Until now this check ended one line
            # above, at "is it in the governed VOCABULARY" -- and the
            # vocabulary is not the same set as what an executor can
            # run. INTERFACE_RESET and CLEAR_COUNTERS are fully governed
            # classes with no implementation on any protocol this
            # platform ships, so an agent could be bound to one, propose
            # it, get a human approval, have a directive dispatched, and
            # be refused by the node. Every time, with nothing upstream
            # able to say why.
            #
            # This is a PLATFORM fact, not a fleet fact: no device
            # anywhere can run these, so the refusal needs no database
            # read and applies before any scope is resolved.
            if not _PLATFORM_FACTS[ref]["implemented"]:
                raise HTTPException(
                    400,
                    f"{ref} is a governed action class that no executor in "
                    f"this platform implements, so binding it would create "
                    f"an agent that can only ever propose actions the node "
                    f"will refuse. Its risk level, preconditions and "
                    f"blast-radius semantics are intact and it stays in the "
                    f"vocabulary; implementing it is a separate governed "
                    f"capability slice.",
                )
        elif binding.kind == KIND_READ:
            if binding.capability_ref.lower() not in READ_CAPABILITIES:
                raise HTTPException(
                    400,
                    f"{binding.capability_ref!r} is not a governed read "
                    f"capability ({', '.join(sorted(READ_CAPABILITIES))})",
                )


async def _refuse_zero_reach(
    session: AsyncSession,
    tenant_id: str,
    agent_id: str,
    capabilities: list[CapabilityBinding],
) -> None:
    """Refuse a binding no device in the agent's OWN scope can execute.

    `_validate_capabilities` catches the platform-wide case (nothing
    implements this class anywhere). This catches the fleet case: the
    class is implemented, but not by anything this particular agent can
    reach -- an agent scoped to servers and bound to INTERFACE_DISABLE,
    or to a site whose switches do not permit it on their allow list.
    Left unrefused, that agent looks correctly configured and proposes
    nothing, or proposes and is refused at the node forever.

    Runs AFTER the scope rows are written, and reaches the devices through
    `resolve_scope` -- the SAME function the evaluator and `agent_view`
    use to decide what an agent sees. That matters more than it looks:
    the repository's E1.2 read filter is site-based, so a `device_class`
    or `device` scope returns nothing through it, and this check would
    then read "no devices in scope yet" and wave through a binding the
    evaluator will never act on. Two notions of "in scope" is exactly the
    divergence this codebase keeps paying for, so there is one.

    Org-unit scopes still expand through `load_agent_scope` -- the ONE
    scope resolver -- before `resolve_scope` flattens them, which is how
    the runtime does it too. Nothing is committed yet, so raising here
    leaves no agent behind.

    REFUSES ON CAPABILITY, NEVER ON POLICY. The test is whether the
    devices' PROTOCOLS implement the class, not whether their allow lists
    currently permit it. An earlier version of this check used the
    effective set and the compose gate caught it: the A0+A1 gate binds
    SEL_CLEAR deliberately, to a demo node whose allow list does not
    carry it, precisely to prove that the node's own refusal is final and
    becomes attributed evidence. Refusing that binding here would have
    promoted a mutable node policy into a hard Central Command
    configuration constraint -- an operator could no longer configure an
    agent ahead of a config rollout, and the Registry would be answering
    question six, which belongs to the node.

    UNKNOWN NEVER REFUSES. A device that has not declared could turn out
    to be capable, and a fleet mid-upgrade is entirely undeclared; only
    provable zero reach -- every in-scope device declared, none of them
    IMPLEMENTING the class -- is a refusal.
    """
    wanted = [
        b.capability_ref.upper()
        for b in capabilities
        if b.kind == KIND_ACTION_CLASS
    ]
    if not wanted:
        return
    agent_scope = await load_agent_scope(
        session, tenant_id=tenant_id, agent_id=agent_id
    )
    scope_rules = await OperationalAgentRepo(session).list_scopes(agent_id)
    devices = resolve_scope(
        scope_rules,
        await FleetCacheRepo(session).list_all(tenant_id),
        agent_scope.site_ids,
    )
    reach = reachable_action_classes(devices)
    if reach["devices"] == 0 or reach["unknown"]:
        # No devices in scope yet, or some have not declared. An agent
        # built before its fleet arrives is legitimate; refusing it
        # would make the Registry an obstacle rather than a truth.
        return
    for ref in wanted:
        if ref not in reach["implemented"]:
            raise HTTPException(
                400,
                f"no device in this agent's scope can execute {ref}. "
                f"{reach['devices']} device(s) are in scope and every one "
                f"has declared its capabilities; none of their protocols "
                f"implements this class, so no allow-list change could "
                f"make it runnable here. Widen the agent's scope, or bind "
                f"a class these devices can actually perform.",
            )


def _enforce_delegation_ceiling(creator_scope, scopes) -> None:
    """An agent may never reach further than the human who built it. E1.2.

    This IS the authorization gate for building an agent, not a check
    layered on top of one. Requiring tenant scope to create an agent
    would make the ceiling unreachable -- a tenant-wide creator can
    delegate anything -- so the gate is per requested scope row instead:
    a region owner may build an agent for their region, and nobody may
    build one that reaches past themselves.

    "Delegated administration cannot exceed the delegator's authority",
    as arithmetic rather than as review.
    """
    if not scopes:
        # No rows means no devices (A0). Nothing to cap, and the agent
        # is inert until somebody with the authority binds a scope.
        return
    for rule in scopes:
        if not _scope_rule_within(creator_scope, rule):
            raise HTTPException(
                status_code=403,
                detail=(
                    f"cannot grant this agent {rule.scope_type} "
                    f"{rule.scope_ref!r}: it is outside your own authorized "
                    "scope, and an agent may never reach further than the "
                    "person who created it"
                ),
            )


def _scope_rule_within(creator_scope, rule) -> bool:
    """Does the creator hold `site.manage` over this requested scope?

    Note what does NOT count: contextual visibility. A cluster manager
    who can see Region West as a breadcrumb cannot bind an agent to it,
    because `permits` reads the authority grants and never
    `contextual_unit_ids`.
    """
    if rule.scope_type == SCOPE_SITE:
        return creator_scope.permits("site.manage", site_id=rule.scope_ref)
    if rule.scope_type == SCOPE_ORG_UNIT:
        path = creator_scope.unit_paths.get(rule.scope_ref, "")
        return bool(path) and creator_scope.permits(
            "site.manage", org_unit_path=path
        )
    if rule.scope_type == SCOPE_DEVICE:
        return creator_scope.permits(
            "site.manage", device_agent_id=rule.scope_ref
        )
    # `device_class` spans the whole fleet, so only a tenant-wide
    # principal may delegate one. Anything narrower would silently widen.
    return creator_scope.permits("site.manage", tenant_object=True)


async def _agent_scope_rules(repo, agent_id):
    """The agent's CURRENT scope rows, as rule-shaped objects."""
    return [
        ScopeRule(scope_type=row.scope_type, scope_ref=row.scope_ref)
        for row in await repo.list_scopes(agent_id)
    ]


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
    scope=Depends(get_scope),
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
    scope=Depends(get_scope),
) -> dict:
    """Create an agent. It starts in `draft` and evaluates nothing.

    Activation is a separate, human, audited act: creating a bundle and
    turning it loose must never be the same request.
    """
    # E1.2 layer 3. The ceiling IS the gate: authority over every scope
    # the agent is asked to reach, and nothing wider.
    _enforce_delegation_ceiling(scope, body.scopes)
    repo = OperationalAgentRepo(session)
    if await repo.get_by_name(user.tenant_id, body.name) is not None:
        raise HTTPException(409, f"an agent named {body.name!r} already exists")
    await _validate_scopes(session, user.tenant_id, body.scopes)
    _validate_capabilities(body.capabilities)
    if body.budget_period not in BUDGET_PERIODS:
        raise HTTPException(
            400, f"budget_period must be one of {list(BUDGET_PERIODS)}",
        )

    actor = user.email or user.user_id
    agent = await repo.create(
        tenant_id=user.tenant_id,
        name=body.name,
        description=body.description,
        autonomy_ceiling=body.autonomy_ceiling,
        require_approval_always=body.require_approval_always,
        max_proposals_per_day=body.max_proposals_per_day,
        execution_budget=body.execution_budget,
        budget_period=body.budget_period,
        created_by=actor,
    )
    await _apply_bindings(session, repo, agent, body.scopes, body.capabilities)
    # Capability Registry: scope rows exist now, so the agent's own
    # reach is resolvable through the one resolver. Nothing is
    # committed yet, so a refusal here leaves no agent behind.
    await _refuse_zero_reach(
        session, user.tenant_id, agent.id, body.capabilities
    )
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
    scope=Depends(get_scope),
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
        resolved_site_ids=(
            await load_agent_scope(
                session, tenant_id=user.tenant_id, agent_id=agent.id
            )
        ).site_ids,
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
    scope=Depends(get_scope),
) -> dict:
    agent = await _require_agent(session, user.tenant_id, agent_id)
    if agent.status == STATUS_RETIRED:
        raise HTTPException(409, "a retired agent cannot be reconfigured")
    repo = OperationalAgentRepo(session)
    # E1.2: authority over what this agent already reaches. An agent
    # nobody can reach is an agent nobody may reconfigure.
    _enforce_delegation_ceiling(scope, await _agent_scope_rules(repo, agent.id))
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
    # A2/D2: the budget is configuration -- it changes what this agent may
    # do without a human, so it is version-bound like everything else and
    # re-opens the preflight.
    if body.execution_budget is not None:
        agent.execution_budget = body.execution_budget
        changed["execution_budget"] = body.execution_budget
    if body.budget_period is not None:
        if body.budget_period not in BUDGET_PERIODS:
            raise HTTPException(
                400, f"budget_period must be one of {list(BUDGET_PERIODS)}",
            )
        agent.budget_period = body.budget_period
        changed["budget_period"] = body.budget_period

    # The PAUSE is not configuration. It is a runtime safety control that
    # can only tighten, so it deliberately does NOT bump the version:
    # versioning it would mean an emergency pause invalidated a valid
    # activation approval, and resuming needed a fresh one. A control
    # that is expensive to use in an emergency does not get used.
    paused_change: Optional[str] = None
    if body.paused_reason is not None:
        agent.paused_reason = body.paused_reason
        paused_change = body.paused_reason or ""

    if not changed and paused_change is None:
        raise HTTPException(400, "no fields to update")
    if changed:
        await repo.bump_version(agent, actor)
    if paused_change is not None:
        await AuditRepo(session).append(
            actor=actor,
            action=(
                "operational_agent.paused" if paused_change
                else "operational_agent.resumed"
            ),
            subject=agent.id,
            tenant_id=user.tenant_id,
            detail={"reason": paused_change, "version": agent.version},
        )
    if changed:
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
    scope=Depends(get_scope),
) -> dict:
    """Replace what this agent can see and what it can propose."""
    agent = await _require_agent(session, user.tenant_id, agent_id)
    if agent.status == STATUS_RETIRED:
        raise HTTPException(409, "a retired agent cannot be reconfigured")
    repo = OperationalAgentRepo(session)
    # E1.2, BOTH ends, like an org-unit move: authority over what the
    # agent reaches today and over what it is being pointed at. Without
    # the first check a scoped principal could re-aim somebody else's
    # agent; without the second they could widen their own.
    _enforce_delegation_ceiling(scope, await _agent_scope_rules(repo, agent.id))
    _enforce_delegation_ceiling(scope, body.scopes)
    await _validate_scopes(session, user.tenant_id, body.scopes)
    _validate_capabilities(body.capabilities)
    actor = user.email or user.user_id
    await _apply_bindings(session, repo, agent, body.scopes, body.capabilities)
    # Capability Registry: scope rows exist now, so the agent's own
    # reach is resolvable through the one resolver. Nothing is
    # committed yet, so a refusal here leaves no agent behind.
    await _refuse_zero_reach(
        session, user.tenant_id, agent.id, body.capabilities
    )
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


@router.post("/{agent_id}/preflight",
             dependencies=[Depends(require_permission("site.manage"))])
async def preflight_agent(
    agent_id: str,
    request: Request,
    user: UserContext = Depends(require_permission("site.manage")),
    session: AsyncSession = Depends(get_session),
    scope=Depends(get_scope),
) -> dict:
    """Produce the activation readiness contract for this configuration.

    Twelve dimensions, each with a verdict a caller can branch on and a
    sentence a person can act on. Assembled SERVER-SIDE and stored: the
    Console consumes this and never recreates it, because a page that
    computed its own verdicts could show an operator something different
    from what the activation gate enforces.
    """
    from harkeniq_cc.agent_lifecycle import run_preflight

    agent = await _require_agent(session, user.tenant_id, agent_id)
    _enforce_delegation_ceiling(scope, await _agent_scope_rules(
        OperationalAgentRepo(session), agent.id))
    result = await run_preflight(
        session, request.app.state.cc, tenant_id=user.tenant_id,
        agent=agent, actor=user.email or user.user_id,
    )
    # D1: raise the activation approval subject now, so an operator sees
    # the decision they will need before they try to activate.
    if result.get("requires_activation_approval"):
        from harkeniq_cc.agent_activation import activation_subject_ref

        agent.activation_subject_ref = activation_subject_ref(
            agent.id, agent.version, result.get("unattended_classes") or [],
        )
    else:
        agent.activation_subject_ref = ""
    await session.commit()
    return result


@router.post("/{agent_id}/acknowledge",
             dependencies=[Depends(require_permission("site.manage"))])
async def acknowledge_agent(
    agent_id: str,
    user: UserContext = Depends(require_permission("site.manage")),
    session: AsyncSession = Depends(get_session),
    scope=Depends(get_scope),
) -> dict:
    """A named human accepts this configuration's warnings and unknowns."""
    from harkeniq_cc.agent_lifecycle import acknowledge_preflight

    agent = await _require_agent(session, user.tenant_id, agent_id)
    try:
        result = await acknowledge_preflight(
            session, tenant_id=user.tenant_id, agent=agent,
            actor=user.email or user.user_id,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    await session.commit()
    return result


@router.get("/{agent_id}/preflight",
            dependencies=[Depends(require_permission("fleet.view"))])
async def get_agent_preflight(
    agent_id: str,
    user: UserContext = Depends(require_permission("fleet.view")),
    session: AsyncSession = Depends(get_session),
    scope=Depends(get_scope),
) -> dict:
    from harkeniq_cc.db.repos import AgentPreflightRepo

    agent = await _require_agent(session, user.tenant_id, agent_id)
    row = await AgentPreflightRepo(session).current(agent.id)
    if row is None:
        return {
            "agent_id": agent.id, "exists": False,
            "configuration_version": int(agent.version),
            "detail": "no preflight has been run for this configuration",
        }
    result = row.result or {}

    # A2/D1: readiness has to say whether the approval it demands has
    # actually been given, and by how many people. Reported from
    # `activation_approval_state` -- E0.1's completion rule -- so the
    # page can never show "approved" where the gate would refuse.
    activation_approval = None
    if result.get("requires_activation_approval") and agent.activation_subject_ref:
        from harkeniq_cc.api.approvals import activation_approval_state

        block = await activation_approval_state(
            session, user.tenant_id, agent.activation_subject_ref,
        )
        activation_approval = {
            "subject_ref": agent.activation_subject_ref,
            **block,
            "note": (
                "Decided on the approvals queue, under action.approve, on the "
                "same ledger a node action uses. Approving authorizes "
                "activation; a person still activates."
            ),
        }

    return {
        "agent_id": agent.id, "exists": True,
        "current": int(row.configuration_version) == int(agent.version),
        "produced_by": row.produced_by,
        "produced_at": row.produced_at.isoformat() if row.produced_at else None,
        "acknowledged_by": agent.activation_acknowledged_by or None,
        "acknowledgement_current": (
            bool(agent.activation_acknowledged_by)
            and int(agent.activation_acknowledged_version or 0) == int(agent.version)
        ),
        "activation_approval": activation_approval,
        **result,
    }


@router.get("/{agent_id}/runtime",
            dependencies=[Depends(require_permission("fleet.view"))])
async def agent_runtime(
    agent_id: str,
    user: UserContext = Depends(require_permission("fleet.view")),
    session: AsyncSession = Depends(get_session),
    scope=Depends(get_scope),
) -> dict:
    """What the runtime can HONESTLY say about this agent.

    Only signals the platform actually produces. A dimension it cannot
    observe reads unknown rather than being filled with a plausible
    value, because an operator acts on what this says.
    """
    from harkeniq_cc.agent_lifecycle import runtime_state

    agent = await _require_agent(session, user.tenant_id, agent_id)
    return await runtime_state(session, tenant_id=user.tenant_id, agent=agent)


async def _activation_decision(session, tenant_id: str, agent) -> dict:
    """Has activating THIS configuration been approved? (D1, A19.5.)

    The subject is a digest over the agent, its configuration version
    and the classes activation would let run unattended -- so an
    approval cannot survive an edit that changes any of them.

    The verdict comes from `activation_approval_state`, which is E0.1's
    completion rule and nothing else: same policy resolution, same
    required-approver count, same group membership rule, same terminal
    denial a node action gets. This function deliberately does NOT read
    approval records and decide for itself.

    It used to. It counted any single record as approval, so a tenant
    with `required_approvers = 2` got single authorization for
    activation -- E0.1's own defect, reintroduced at the fourth origin.
    One ledger, one completion rule; a second implementation here is a
    defect whatever it computes.
    """
    from harkeniq_cc.api.approvals import activation_approval_state

    subject_ref = agent.activation_subject_ref or ""
    if not subject_ref:
        # No subject raised means preflight has not run for this
        # configuration, or it needs no approval. Either way this is not
        # an approval, and the caller's preflight check decides which.
        return {"state": "pending", "required": 1, "received": 0}
    return await activation_approval_state(session, tenant_id, subject_ref)


@router.get(
    "/{agent_id}/proposals",
    dependencies=[Depends(require_permission("fleet.view"))],
)
async def list_proposals(
    agent_id: str,
    limit: int = Query(100, ge=1, le=500),
    user: UserContext = Depends(require_permission("fleet.view")),
    session: AsyncSession = Depends(get_session),
    scope=Depends(get_scope),
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


# ---------------------------------------------------------------------------
# A3: machine identity (spec A20)
# ---------------------------------------------------------------------------
#
# The operator-facing surface lives HERE, with the agent, because this is
# where `site.manage` and the E1.2 delegation ceiling are enforced --
# whoever may build and activate an agent may credential it, and the
# credential grants nothing by itself (A20.2). Keycloak provisioning
# happens at the Console over the existing internal channel; these routes
# hold the policy, not the plumbing.


def _identity_dict(row, agent=None) -> dict:
    """What an operator may see. Never the secret — it is not stored."""
    return {
        "agent_id": row.agent_id,
        "client_id": row.keycloak_client_id,
        "realm": row.realm,
        "status": row.status,
        "issued_by": row.issued_by,
        "issued_at": row.issued_at.isoformat() if row.issued_at else None,
        "rotated_at": row.rotated_at.isoformat() if row.rotated_at else None,
        "rotated_by": row.rotated_by or None,
        "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
        "revoked_by": row.revoked_by or None,
        "revoke_reason": row.revoke_reason or None,
        "last_seen_at": row.last_seen_at.isoformat() if row.last_seen_at else None,
        "last_seen_source": row.last_seen_source or None,
        "contract": (
            "A machine identity answers 'who is this runtime?'. It grants no "
            "permission, scope, capability, autonomy, approval or execution "
            "authority; an authenticated agent is capped at fleet.view and "
            "incident.view and still proposes through the same governed funnel."
        ),
    }


@router.post(
    "/{agent_id}/identity",
    dependencies=[Depends(require_permission("site.manage"))],
)
async def issue_identity(
    agent_id: str,
    request: Request,
    user: UserContext = Depends(require_permission("site.manage")),
    session: AsyncSession = Depends(get_session),
    scope=Depends(get_scope),
) -> dict:
    """Issue this agent's machine identity. The secret is shown ONCE.

    A20.5: Central Command never stores the secret — there is no column
    it could be written into. Keycloak holds it; if it is lost, rotate.
    """
    from harkeniq_cc import identity_client
    from harkeniq_cc.db.repos import AgentIdentityRepo
    from harkeniq_cc.machine_identity import client_id_for

    agent = await _require_agent(session, user.tenant_id, agent_id)
    repo = OperationalAgentRepo(session)
    _enforce_delegation_ceiling(scope, await _agent_scope_rules(repo, agent.id))
    if agent.status == STATUS_RETIRED:
        raise HTTPException(409, "a retired agent cannot be credentialed")

    identities = AgentIdentityRepo(session)
    if await identities.get_for_agent(user.tenant_id, agent.id) is not None:
        raise HTTPException(
            409,
            "this agent already has a machine identity; rotate it rather than "
            "issuing a second one — two identities would be two answers to "
            "'who is this runtime?'",
        )

    state = request.app.state.cc
    realm = getattr(state.config, "keycloak_realm", "") or ""
    client_id = client_id_for(agent.id)
    result, reason = await identity_client.provision(
        state, realm=realm, client_id=client_id,
    )
    actor = user.email or user.user_id
    if result is None:
        # Fails CLOSED and audited: a half-issued identity that nobody
        # recorded is worse than none at all.
        await AuditRepo(session).append(
            actor=actor, action="agent_identity.issue_failed",
            subject=agent.id, tenant_id=user.tenant_id,
            detail={"reason": reason, "client_id": client_id},
        )
        await session.commit()
        raise HTTPException(502, f"machine identity could not be issued: {reason}")

    row = await identities.create(
        tenant_id=user.tenant_id, agent_id=agent.id, realm=realm,
        keycloak_client_id=client_id,
        keycloak_sub=str(result.get("subject", "")),
        issued_by=actor,
    )
    await AuditRepo(session).append(
        actor=actor, action="agent_identity.issued",
        subject=agent.id, tenant_id=user.tenant_id,
        detail={"client_id": client_id, "realm": realm,
                "subject": str(result.get("subject", ""))},
    )
    await session.commit()
    return {
        **_identity_dict(row, agent),
        # Once. Never stored, never shown again.
        "client_secret": result.get("secret", ""),
        "secret_notice": (
            "Store this now. Central Command does not keep it and cannot show "
            "it again; if it is lost, rotate the identity."
        ),
    }


@router.post(
    "/{agent_id}/identity/rotate",
    dependencies=[Depends(require_permission("site.manage"))],
)
async def rotate_identity(
    agent_id: str,
    request: Request,
    user: UserContext = Depends(require_permission("site.manage")),
    session: AsyncSession = Depends(get_session),
    scope=Depends(get_scope),
) -> dict:
    """Rotate the secret. No execution gap, and never a second identity.

    One client, one subject, one row — only the secret changes, so tokens
    already issued stay valid to their natural expiry while the agent
    picks up the new secret on its next fetch.
    """
    from harkeniq_cc import identity_client
    from harkeniq_cc.db.repos import AgentIdentityRepo

    agent = await _require_agent(session, user.tenant_id, agent_id)
    repo = OperationalAgentRepo(session)
    _enforce_delegation_ceiling(scope, await _agent_scope_rules(repo, agent.id))

    identities = AgentIdentityRepo(session)
    row = await identities.get_for_agent(user.tenant_id, agent.id)
    if row is None:
        raise HTTPException(404, "this agent has no machine identity")
    if row.status != "active":
        raise HTTPException(
            409, f"a {row.status} identity cannot be rotated; issue a new one",
        )

    state = request.app.state.cc
    result, reason = await identity_client.rotate(
        state, realm=row.realm, client_id=row.keycloak_client_id,
    )
    actor = user.email or user.user_id
    if result is None:
        raise HTTPException(502, f"rotation failed: {reason}")

    await identities.mark_rotated(row, actor)
    await AuditRepo(session).append(
        actor=actor, action="agent_identity.rotated",
        subject=agent.id, tenant_id=user.tenant_id,
        detail={"client_id": row.keycloak_client_id},
    )
    await session.commit()
    return {
        **_identity_dict(row, agent),
        "client_secret": result.get("secret", ""),
        "secret_notice": (
            "The previous secret no longer works. Tokens already issued remain "
            "valid until they expire, so there is no execution gap."
        ),
    }


class RevokeIdentityBody(BaseModel):
    reason: str = Field("", max_length=512)


@router.post(
    "/{agent_id}/identity/revoke",
    dependencies=[Depends(require_permission("site.manage"))],
)
async def revoke_identity(
    agent_id: str,
    request: Request,
    body: RevokeIdentityBody = Body(default_factory=RevokeIdentityBody),
    user: UserContext = Depends(require_permission("site.manage")),
    session: AsyncSession = Depends(get_session),
    scope=Depends(get_scope),
) -> dict:
    """Revoke immediately (A20.5).

    Two things happen, and only one of them is fast: Keycloak stops
    issuing NEW tokens, and Central Command's status row refuses the ones
    already out there. Access tokens live 300s, so the row is what makes
    revocation immediate rather than bounded by a token lifetime — which
    is why the row is written even if the Keycloak call fails.
    """
    from harkeniq_cc import identity_client
    from harkeniq_cc.db.repos import AgentIdentityRepo

    agent = await _require_agent(session, user.tenant_id, agent_id)
    repo = OperationalAgentRepo(session)
    _enforce_delegation_ceiling(scope, await _agent_scope_rules(repo, agent.id))

    identities = AgentIdentityRepo(session)
    row = await identities.get_for_agent(user.tenant_id, agent.id)
    if row is None:
        raise HTTPException(404, "this agent has no machine identity")

    actor = user.email or user.user_id
    reason = body.reason or "revoked by an operator"
    # The row FIRST, and unconditionally: a Keycloak outage must not
    # leave a credential the operator believes they revoked.
    await identities.mark_revoked(row, actor, reason)
    _, kc_reason = await identity_client.set_enabled(
        request.app.state.cc, realm=row.realm,
        client_id=row.keycloak_client_id, enabled=False,
    )
    await AuditRepo(session).append(
        actor=actor, action="agent_identity.revoked",
        subject=agent.id, tenant_id=user.tenant_id,
        detail={"client_id": row.keycloak_client_id, "reason": reason,
                "keycloak_disabled": not kc_reason,
                "keycloak_detail": kc_reason or None},
    )
    await session.commit()
    return {
        **_identity_dict(row, agent),
        "effective": "immediate",
        "detail": (
            "Refused at Central Command from the next request. "
            + ("Keycloak client disabled." if not kc_reason
               else f"Keycloak could not be reached ({kc_reason}); the identity "
                    "is still refused here, so no token works.")
        ),
    }


@router.get(
    "/{agent_id}/identity",
    dependencies=[Depends(require_permission("fleet.view"))],
)
async def get_identity(
    agent_id: str,
    user: UserContext = Depends(require_permission("fleet.view")),
    session: AsyncSession = Depends(get_session),
    scope=Depends(get_scope),
) -> dict:
    """Identity status. Read-only, and never the secret."""
    from harkeniq_cc.db.repos import AgentIdentityRepo

    agent = await _require_agent(session, user.tenant_id, agent_id)
    row = await AgentIdentityRepo(session).get_for_agent(user.tenant_id, agent.id)
    if row is None:
        return {
            "agent_id": agent.id, "exists": False,
            "detail": "this agent has no machine identity",
        }
    return {"exists": True, **_identity_dict(row, agent)}


# ---------------------------------------------------------------------------
# The catch-all transition route — REGISTERED LAST, deliberately.
# ---------------------------------------------------------------------------
#
# `/{agent_id}/{transition}` matches any single path segment, and Starlette
# matches routes in REGISTRATION order. Declared earlier in the module it
# swallowed `POST /{agent_id}/identity` and answered 404 'unknown
# transition' — a route that exists, guarded correctly, and unreachable.
#
# Any new single-segment route under this prefix must be declared ABOVE
# this one. A route-contract test asserts every declared route is
# reachable, so a future collision fails the suite rather than 404ing in
# production.
@router.post(
    "/{agent_id}/{transition}",
    dependencies=[Depends(require_permission("site.manage"))],
)
async def transition_agent(
    agent_id: str,
    transition: str,
    request: Request,
    user: UserContext = Depends(require_permission("site.manage")),
    session: AsyncSession = Depends(get_session),
    scope=Depends(get_scope),
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
    repo = OperationalAgentRepo(session)
    # E1.2: authority over what this agent already reaches. An agent
    # nobody can reach is an agent nobody may activate, pause or retire.
    _enforce_delegation_ceiling(scope, await _agent_scope_rules(repo, agent.id))
    if agent.status not in allowed_from:
        raise HTTPException(
            409,
            f"cannot {transition} an agent in status {agent.status!r}",
        )
    if target == STATUS_ACTIVE:
        # A2: activation is gated on a STORED preflight for THIS exact
        # configuration version. The two ad-hoc checks that used to live
        # here are now two of its twelve dimensions, so there is one
        # readiness contract and the Console cannot disagree with it.
        from harkeniq_cc.agent_activation import may_activate
        from harkeniq_cc.db.repos import AgentPreflightRepo

        row = await AgentPreflightRepo(session).current(agent.id)
        allowed, reason = may_activate(
            (row.result if row is not None else None), agent
        )
        if not allowed:
            raise HTTPException(409, reason)

        result = row.result or {}
        # D1: activation approval is required only where activation
        # confers real unattended execution. A propose-only agent grants
        # no new authority by being switched on, so gating it would be
        # ceremony.
        if result.get("requires_activation_approval"):
            block = await _activation_decision(session, user.tenant_id, agent)
            if block.get("state") != STATE_APPROVED:
                raise HTTPException(
                    409,
                    "this configuration grants unattended execution for "
                    + ", ".join(result.get("unattended_classes") or [])
                    + f", so activation requires approval on the approvals "
                    f"queue before it can proceed "
                    f"({block.get('received', 0)} of "
                    f"{block.get('required', 1)} approval(s) recorded"
                    + (", denied" if block.get("state") == "denied" else "")
                    + ")",
                )
    actor = user.email or user.user_id
    await repo.set_status(agent, target, actor)

    # A20.7: retiring an agent revokes its machine identity. An identity
    # that outlived the agent it names would answer "who is this
    # runtime?" with the name of something that no longer exists.
    if target == STATUS_RETIRED:
        from harkeniq_cc import identity_client
        from harkeniq_cc.db.repos import AgentIdentityRepo

        identities = AgentIdentityRepo(session)
        identity = await identities.get_for_agent(user.tenant_id, agent.id)
        if identity is not None and identity.status == "active":
            await identities.mark_revoked(
                identity, actor, "the agent was retired", status="retired",
            )
            _, kc_reason = await identity_client.set_enabled(
                request.app.state.cc, realm=identity.realm,
                client_id=identity.keycloak_client_id, enabled=False,
            )
            await AuditRepo(session).append(
                actor=actor, action="agent_identity.retired",
                subject=agent.id, tenant_id=user.tenant_id,
                detail={"client_id": identity.keycloak_client_id,
                        "keycloak_disabled": not kc_reason,
                        "keycloak_detail": kc_reason or None},
            )

    await AuditRepo(session).append(
        actor=actor,
        action=f"operational_agent.{transition}d",
        subject=agent.id,
        tenant_id=user.tenant_id,
        detail={
            "status": target,
            "version": agent.version,
            # A19.9: what was actually switched on, in the record itself.
            "activated_version": int(agent.activated_version or 0),
        },
    )

    # A19.11: activation is the install trigger. Bound skills are
    # delivered to the devices in the agent's own scope that can actually
    # run what they recommend -- per device, deduplicated by a durable
    # ledger, so re-activating never installs twice.
    installs: dict = {}
    if target == STATUS_ACTIVE:
        from harkeniq_cc.agent_lifecycle import install_bound_skills

        try:
            installs = await install_bound_skills(
                session, request.app.state.cc, tenant_id=user.tenant_id,
                agent=agent, preflight=(row.result or {}), actor=actor,
            )
        except Exception as exc:  # noqa: BLE001
            # A site being unreachable must not roll back an activation a
            # human just authorized: the ledger records what was queued,
            # and an operator can see what did not reach its devices.
            logger.warning(
                "skill install during activation of %s failed: %s", agent.id, exc,
            )
            installs = {"installed": 0, "skipped": 0, "skills": [],
                        "error": str(exc)[:256]}

    await session.commit()
    payload = _agent_dict(
        agent,
        await repo.list_scopes(agent.id),
        await repo.list_capabilities(agent.id),
    )
    if target == STATUS_ACTIVE:
        payload["skill_installs"] = installs
    return payload

