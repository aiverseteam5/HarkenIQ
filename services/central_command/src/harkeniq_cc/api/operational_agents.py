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

from datetime import datetime, timedelta, timezone

from fastapi import (
    APIRouter, Body, Depends, HTTPException, Query, Request, Response,
)
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_cc.api.deps import (
    forbid_out_of_scope, get_current_user, get_scope, get_session,
    require_permission,
)
from harkeniq_cc.actor import actor_of
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
    INGRESS_CAPABILITIES,
    KIND_ACTION_CLASS,
    KIND_INGRESS,
    KIND_READ,
    KIND_SKILL,
    PROPOSAL_AWAITING,
    READ_CAPABILITIES,
    REQUIRED_READS,
    SCOPE_DEVICE,
    SCOPE_DEVICE_CLASS,
    SCOPE_SITE,
    SCOPE_TYPES,
    STATUS_ACTIVE,
    STATUS_DRAFT,
    STATUS_PAUSED,
    STATUS_RETIRED,
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
        elif binding.kind == KIND_INGRESS:
            # A24.4. Validated like a read, and for a sharper reason: an
            # unrecognised ingress ref would map to no permission, so the
            # binding would be accepted, rendered, and grant NOTHING --
            # an agent configured to submit that silently cannot. That is
            # the accepted-and-inert shape E0.3 refused for skills.
            if binding.capability_ref.lower() not in INGRESS_CAPABILITIES:
                raise HTTPException(
                    400,
                    f"{binding.capability_ref!r} is not a governed ingress "
                    f"capability ({', '.join(sorted(INGRESS_CAPABILITIES))})",
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


def _agent_visible(scope, rules) -> bool:
    """May this caller READ this agent? (A23, READ_SCOPED made true.)

    An agent is a scoped object: visible to a caller who reaches at
    least one of its scope rules with `fleet.view`. An agent with no
    rules reaches nothing and is a tenant-level object, visible to a
    tenant-wide reader only. Out of scope is absent (404), never 403 --
    a 403 confirms the agent exists.
    """
    if getattr(scope, "tenant_wide", False):
        return True
    return any(_scope_rule_within(scope, r, "fleet.view") for r in rules)


async def _require_visible_agent(session, tenant_id: str, agent_id: str, scope):
    """The agent, or 404 if it does not exist OR the caller cannot see it."""
    agent = await _require_agent(session, tenant_id, agent_id)
    rules = await OperationalAgentRepo(session).list_scopes(agent.id)
    if not _agent_visible(scope, rules):
        raise HTTPException(404, "operational agent not found")
    return agent


def _narrow_proposals(scope, proposals):
    """A scoped reader sees the proposals made at THEIR sites."""
    if getattr(scope, "tenant_wide", False):
        return list(proposals)
    visible = set(getattr(scope, "site_ids", ()) or ())
    return [p for p in proposals if p.site_id and p.site_id in visible]


def _scope_rule_within(creator_scope, rule, permission: str = "site.manage") -> bool:
    """Does the caller hold `permission` over this scope rule?

    ONE implementation, asked by the delegation ceiling with
    `site.manage` and by A5's dry-run with `fleet.view`. A hand-written
    second copy is what made S6's headline org-unit case a 500.

    Note what does NOT count: contextual visibility. A cluster manager
    who can see Region West as a breadcrumb cannot bind an agent to it,
    because `permits` reads the authority grants and never
    `contextual_unit_ids`.
    """
    if rule.scope_type == SCOPE_SITE:
        return creator_scope.permits(permission, site_id=rule.scope_ref)
    if rule.scope_type == SCOPE_ORG_UNIT:
        path = creator_scope.unit_paths.get(rule.scope_ref, "")
        return bool(path) and creator_scope.permits(
            permission, org_unit_path=path
        )
    if rule.scope_type == SCOPE_DEVICE:
        return creator_scope.permits(
            permission, device_agent_id=rule.scope_ref
        )
    # `device_class` spans the whole fleet, so only a tenant-wide
    # principal may delegate one. Anything narrower would silently widen.
    return creator_scope.permits(permission, tenant_object=True)


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
    actor: str = "",
) -> None:
    # A23-3: scope rows are revoked and revived, never deleted -- the one
    # lifecycle the grant table has for humans, now for agents too.
    await repo.clear_scopes(agent.id, revoked_by=actor)
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
    scope=Depends(get_scope),
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
    # A23: the scope options are what THIS caller may bind -- their own
    # sites and devices, not the tenant's inventory.
    sites = await SiteRepo(session).list_all(user.tenant_id, scope=scope)
    devices = await FleetCacheRepo(session).list_all(user.tenant_id, scope=scope)

    # Which conditions THIS TENANT has a remediation mapped for. A4: read
    # from the catalogue, not a module constant, so the answer to "why
    # would binding this class ever fire?" is the same object an operator
    # can see and change.
    from harkeniq_cc.capability_catalogue import SUBSYSTEM_UNREACHABLE
    from harkeniq_cc.db.repos import CapabilityCatalogueRepo

    triggers: dict[str, list[str]] = {}
    for row in await CapabilityCatalogueRepo(session).list_for_tenant(
        user.tenant_id
    ):
        if not row.enabled:
            continue
        label = ("unreachable management controller"
                 if row.subsystem == SUBSYSTEM_UNREACHABLE else row.subsystem)
        triggers.setdefault(row.action_type, []).append(label)

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
        # A24.4: the one binding kind that implies a write. Listed here
        # because a binding nobody can discover is a binding nobody can
        # create -- and reported SEPARATELY from reads, never merged into
        # them, because granting an agent the ability to ask for work is
        # a different decision from letting it read.
        "ingress_capabilities": [
            {
                "ref": ref,
                "description": desc,
                "required": False,
                "grants_permission": "proposal.submit",
                "note": (
                    "Lets this agent submit candidates it was already "
                    "shown. It confers no authority to decide, approve, "
                    "dispatch or execute: every submission is re-derived "
                    "and re-governed on receipt."
                ),
            }
            for ref, desc in sorted(INGRESS_CAPABILITIES.items())
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
        # A23 (READ_SCOPED, made true): an agent the caller cannot reach
        # is absent from the list, and proposal counts cover the
        # caller's sites only.
        if not _agent_visible(scope, scopes):
            continue
        caps = await repo.list_capabilities(agent.id)
        proposals = _narrow_proposals(
            scope, await prop_repo.list_for_agent(user.tenant_id, agent.id)
        )
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
    await _apply_bindings(
        session, repo, agent, body.scopes, body.capabilities, actor=user.user_id,
    )
    # Capability Registry: scope rows exist now, so the agent's own
    # reach is resolvable through the one resolver. Nothing is
    # committed yet, so a refusal here leaves no agent behind.
    await _refuse_zero_reach(
        session, user.tenant_id, agent.id, body.capabilities
    )
    await AuditRepo(session).append(
        actor=actor,
        actor_ref=actor_of(user),
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
    agent = await _require_visible_agent(session, user.tenant_id, agent_id, scope)
    repo = OperationalAgentRepo(session)
    scopes = await repo.list_scopes(agent.id)
    caps = await repo.list_capabilities(agent.id)
    # A23: a scoped reader sees the agent's reach WITHIN their own scope.
    # The agent may reach further; what it reaches beyond the caller is
    # not the caller's to read.
    devices = await FleetCacheRepo(session).list_all(user.tenant_id, scope=scope)
    contract = await load_autonomy_contract(
        session,
        tenant_id=user.tenant_id,
        actor_id=attribution_key(agent.id, agent.version),
        actor_species="agent",
        permissions=user.permissions,
    )
    from harkeniq_cc.autonomy import narrow_to_sites

    contract = narrow_to_sites(
        contract, None if getattr(scope, "tenant_wide", False) else set(scope.site_ids)
    )
    proposals = _narrow_proposals(
        scope, await AgentProposalRepo(session).list_for_agent(
            user.tenant_id, agent.id,
        )
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
            actor_ref=actor_of(user),
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
            actor_ref=actor_of(user),
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
    await _apply_bindings(
        session, repo, agent, body.scopes, body.capabilities, actor=user.user_id,
    )
    # Capability Registry: scope rows exist now, so the agent's own
    # reach is resolvable through the one resolver. Nothing is
    # committed yet, so a refusal here leaves no agent behind.
    await _refuse_zero_reach(
        session, user.tenant_id, agent.id, body.capabilities
    )
    await repo.bump_version(agent, actor)
    await AuditRepo(session).append(
        actor=actor,
        actor_ref=actor_of(user),
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
    repo = OperationalAgentRepo(session)
    # A23 (OBJECT_GATED, made true): acknowledging is a configuration
    # step and sits under the same delegation ceiling as preflight.
    _enforce_delegation_ceiling(scope, await _agent_scope_rules(repo, agent.id))
    try:
        result = await acknowledge_preflight(
            session, tenant_id=user.tenant_id, agent=agent,
            actor=user.email or user.user_id, actor_ref=actor_of(user),
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

    agent = await _require_visible_agent(session, user.tenant_id, agent_id, scope)
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

    agent = await _require_visible_agent(session, user.tenant_id, agent_id, scope)
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
    await _require_visible_agent(session, user.tenant_id, agent_id, scope)
    proposals = _narrow_proposals(
        scope, await AgentProposalRepo(session).list_for_agent(
            user.tenant_id, agent_id, limit=limit,
        )
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
            actor=actor, actor_ref=actor_of(user), action="agent_identity.issue_failed",
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
        actor=actor, actor_ref=actor_of(user), action="agent_identity.issued",
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
        actor=actor, actor_ref=actor_of(user), action="agent_identity.rotated",
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
        actor=actor, actor_ref=actor_of(user), action="agent_identity.revoked",
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

    agent = await _require_visible_agent(session, user.tenant_id, agent_id, scope)
    row = await AgentIdentityRepo(session).get_for_agent(user.tenant_id, agent.id)
    if row is None:
        return {
            "agent_id": agent.id, "exists": False,
            "detail": "this agent has no machine identity",
        }
    return {"exists": True, **_identity_dict(row, agent)}


# ---------------------------------------------------------------------------
# Dry-run: what WOULD this agent do, right now (A22.7)
# ---------------------------------------------------------------------------


@router.get(
    "/{agent_id}/dry-run",
    dependencies=[Depends(require_permission("fleet.view"))],
)
async def dry_run_agent(
    agent_id: str,
    user: UserContext = Depends(require_permission("fleet.view")),
    session: AsyncSession = Depends(get_session),
    scope=Depends(get_scope),
) -> dict:
    """What this agent WOULD propose against current state. Writes nothing.

    The capability A5 exists for: until now you could not ask an agent
    what it would do about an incident without it doing it. `run_once`
    had no trigger and evaluation was cadence-only, so commissioning an
    agent meant switching it on and watching.

    A READ, and a GET, because it writes nothing. It was drafted as a
    POST and the route contract refused it -- "a mutation must resolve
    its target to a scope" -- which was the right answer to the wrong
    shape: the fix is the verb, not an exception carved into an invariant
    that exists to stop exactly this.

    Governed as a read (A22.8): `fleet.view`, which A20.3's machine
    ceiling already carries, so an agent may invoke its OWN dry-run with
    no change to that ceiling. The self-restriction is an object-level
    gate below -- an identity may dry-run its own agent and no other --
    because "which object" is the layer E1.2 assigns that question to,
    and widening the ceiling to reach the same outcome would promote an
    object question into a permission.

    It calls the SAME `govern_proposal` the runtime calls (A22.6). A
    preview that reasoned differently from the runtime would be worse
    than no preview. It creates no proposal, spends no budget, dispatches
    nothing and decides nothing.
    """
    # AGENT_PERMISSIONS from its existing home, deliberately: it and
    # MACHINE_PRINCIPAL_CEILING are two constants holding one value with
    # nothing tying them together (defect D10), and that is a NAMED
    # follow-up, not something A5 fixes in passing while standing next
    # to it. Using the runtime's own constant keeps the preview and the
    # runtime asking the contract the same way.
    from harkeniq_cc.agent_runtime import AGENT_PERMISSIONS, _incidents_by_device
    from harkeniq_cc.capability_catalogue import candidates_for
    from harkeniq_cc.db.repos import (
        AgentProposalRepo, CapabilityCatalogueRepo, FleetCacheRepo,
    )
    from harkeniq_cc.governance import (
        load_agent_scope, load_attention, load_autonomy_contract,
    )
    from harkeniq_cc.machine_identity import is_machine
    from harkeniq_cc.operational_agent import (
        BASIS_AUTONOMOUS, attribution_key, candidate_ref, evaluate,
        resolve_scope,
    )

    tenant_id = user.tenant_id
    agent = await _require_agent(session, tenant_id, agent_id)
    # A23 (READ_SCOPED): a human who cannot see this agent gets 404
    # before the reach check below could 403 and confirm it exists.
    if not is_machine(user):
        if not _agent_visible(
            scope, await OperationalAgentRepo(session).list_scopes(agent_id)
        ):
            raise HTTPException(404, "operational agent not found")

    # A22.8: an agent reasons about ITSELF and nothing else. `user_id` is
    # the agent id for a machine principal, which is what makes this one
    # comparison rather than a second identity model.
    if is_machine(user) and user.user_id != agent_id:
        raise HTTPException(
            status_code=403,
            detail=(
                "a machine identity may dry-run its own agent and no other"
            ),
        )

    repo = OperationalAgentRepo(session)
    scopes = await repo.list_scopes(agent_id)

    # A preview shows what the agent would do across ITS OWN reach, which
    # may span sites this caller cannot operate. Narrowing the answer to
    # the caller would be worse than refusing it -- a partial preview is
    # not what the agent would do -- so the caller must be able to reach
    # every site the agent's scope names. Same rule activation applies,
    # and asked explicitly rather than falling through to the tenant
    # question on an empty site id (the A2 completion-slice finding).
    if not is_machine(user):
        for row in scopes:
            if not _scope_rule_within(scope, row, "fleet.view"):
                raise HTTPException(
                    status_code=403,
                    detail=(
                        f"this agent reaches {row.scope_type} "
                        f"{row.scope_ref!r}, which is outside your authorized "
                        "scope; a preview shows what the agent would do "
                        "across its whole reach, and narrowing it would not "
                        "be what the agent would do"
                    ),
                )

    devices = await FleetCacheRepo(session).list_all(tenant_id)
    incidents = await _incidents_by_device(session, tenant_id)
    caps = await repo.list_capabilities(agent_id)
    agent_scope = await load_agent_scope(
        session, tenant_id=tenant_id, agent_id=agent_id,
    )
    attention = {
        item["agent_id"]: item
        for item in (await load_attention(
            session, tenant_id=tenant_id, scope=agent_scope,
        ))["items"]
    }
    contract = await load_autonomy_contract(
        session,
        tenant_id=tenant_id,
        actor_id=attribution_key(agent_id, agent.version),
        actor_species="agent",
        permissions=AGENT_PERMISSIONS,
    )
    catalogue_rows = await CapabilityCatalogueRepo(session).list_for_tenant(
        tenant_id
    )
    catalogue = {
        sub: candidates_for(catalogue_rows, sub)
        for sub in {r.subsystem for r in catalogue_rows}
    }
    prop_repo = AgentProposalRepo(session)
    midnight = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )

    # The SAME resolver the evaluator uses, so "in scope" here and "in
    # scope" there cannot drift (the A17 lesson, applied to the preview).
    in_scope = resolve_scope(scopes, devices, agent_scope.site_ids)

    withheld: list[dict] = []
    would_propose = evaluate(
        catalogue=catalogue,
        agent=agent,
        scopes=scopes,
        resolved_site_ids=agent_scope.site_ids,
        capabilities=caps,
        devices=devices,
        incidents_by_device=incidents,
        autonomy_contract=contract,
        attention_by_device=attention,
        open_dedupe_keys=await prop_repo.all_dedupe_keys(tenant_id),
        proposals_today=await prop_repo.count_since(
            tenant_id, agent_id, midnight,
        ),
        withheld=withheld,
    )

    # Read every ORM attribute BEFORE the rollback below: a rollback
    # expires the identity map, and touching an expired attribute would
    # try to lazy-load on a closed transaction.
    agent_version = agent.version
    agent_status = agent.status

    # Nothing above added, flushed or committed. Rolling back is belt and
    # braces: A22.7 says "writes nothing" and the acceptance proves it by
    # table snapshot, so the code should not be the only thing asserting it.
    await session.rollback()

    return {
        "agent_id": agent_id,
        "agent_version": agent_version,
        "actor": attribution_key(agent_id, agent_version),
        "status": agent_status,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": True,
        "wrote": [],
        "devices_in_scope": len(in_scope),
        "would_propose": [
            {
                # A24.3: the handle an external agent submits. Opaque,
                # server-minted, and re-derived on receipt -- it names a
                # candidate, it does not authorize one.
                "candidate_ref": candidate_ref(tenant_id, p["dedupe_key"]),
                "device_agent_id": p["device_agent_id"],
                "site_id": p["site_id"],
                "action_type": p["action_type"],
                # A22.2: the REAL parameters, resolved and validated. This
                # is the field that would have exposed the A4 defect: every
                # proposal used to carry {"reason": ...} whatever the class.
                "params": p["params"],
                "disposition": p["disposition"],
                "disposition_reason": p["disposition_reason"],
                "blocking_conditions": p["blocking_conditions"],
                "authorization_basis": p["authorization_basis"],
                "requires_human": p["authorization_basis"] != BASIS_AUTONOMOUS,
                "rationale": p["rationale"],
                "evidence": p["evidence"],
            }
            for p in would_propose
        ],
        "withheld": withheld,
        "contract": {
            "governs": (
                "This is what the agent WOULD propose. It confers nothing: "
                "a proposal still passes RBAC, scope, the Capability "
                "Registry, the parameter contract, the autonomy contract, "
                "the approval ledger and the node's own funnel."
            ),
            "wrote_nothing": (
                "No proposal was created, no budget spent, no directive "
                "dispatched and no decision recorded."
            ),
            "same_reasoning": (
                "Composed by the same govern_proposal() the runtime calls, "
                "so a preview cannot disagree with what actually happens."
            ),
        },
    }


# ---------------------------------------------------------------------------
# A6-1: external governed submission by reference (A24)
# ---------------------------------------------------------------------------


class SubmitProposal(BaseModel):
    """The closed transport contract (A24.2).

    `extra="forbid"` is the load-bearing line. Every field an external
    party must never author -- `agent_id`, `action_type`, `device`,
    `params`, `disposition`, `authorization_basis`, `status`,
    `decided_by`, autonomy level, approval, site -- is absent, and
    absence here means REJECTED, not ignored.

    That distinction is the whole point. A22.15 recorded, before the
    route existed, that a body able to carry `authorization_basis` would
    be a remote party writing a self-signed, already-approved execution
    order carrying the flag that waives the node's last refusal. A schema
    that silently dropped such a field would still accept the request
    that tried, and nobody would ever learn a client was attempting it.
    """

    model_config = ConfigDict(extra="forbid")

    #: Opaque, server-minted, from this agent's own dry-run. A24.3: a
    #: lookup, never a licence.
    candidate_ref: str = Field(min_length=8, max_length=128)
    #: Caller-generated replay key. Bounded because it is stored.
    idempotency_key: str = Field(min_length=8, max_length=128)
    #: When the runtime observed what prompted this. Reported only --
    #: never an authorization input, and never trusted as a clock.
    observed_at: Optional[datetime] = None
    #: Free text for the human who will approve. Bounded and recorded on
    #: the audit entry; it cannot influence any decision.
    note: str = Field(default="", max_length=512)


@router.post(
    "/{agent_id}/proposals",
    status_code=201,
)
async def submit_proposal(
    agent_id: str,
    body: SubmitProposal,
    response: Response,
    # A24.13: `proposal.submit` is enforced INSIDE the handler, not as a
    # route dependency. Not a weakening -- the same permission, refused
    # with the same 403 -- but a dependency answers before the durable
    # attempt meter is reached, so a valid credential lacking the
    # permission could generate unlimited unmetered refusals. Order of
    # metering changed; authorization did not.
    user: UserContext = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    scope=Depends(get_scope),
) -> dict:
    """Submit a candidate this agent was already shown. A24.

    PROPOSE-BY-REFERENCE. The caller does not construct a proposal: it
    names one Central Command already governed and offered through
    `GET .../dry-run`, and Central Command re-derives everything before
    anything is written. Authorship stays server-side; what the external
    runtime contributes is judgement about WHICH candidate and WHEN,
    which is the whole product value of an external agent.

    A 201 means a proposal now exists. It does NOT mean anything will
    run: the proposal still faces the approval ledger, the dispatch
    gates, the Site Manager's live safety state and the node's own allow
    list, exactly as an internally derived one does.
    """
    from harkeniq_cc.agent_runtime import AGENT_PERMISSIONS, _incidents_by_device
    from harkeniq_cc.capability_catalogue import candidates_for
    from harkeniq_cc.db.repos import (
        AgentIngressAttemptRepo, AgentProposalRepo, AgentSubmissionRepo,
        AuditRepo, CapabilityCatalogueRepo, FleetCacheRepo,
    )
    from harkeniq_cc.ingress_limits import (
        ATTEMPT_WINDOW_S, OUTCOME_ACCEPTED, OUTCOME_CONFLICT,
        OUTCOME_REFUSED, OUTCOME_REJECTED, OUTCOME_REPLAYED, admit_attempt,
        lock_agent_ingress,
    )
    from harkeniq_cc.governance import (
        load_agent_scope, load_attention, load_autonomy_contract,
    )
    from harkeniq_cc.machine_identity import is_machine
    from harkeniq_cc.operational_agent import (
        STATUS_ACTIVE, attribution_key, candidate_ref, evaluate,
    )
    from harkeniq_cc.proposal_admission import ORIGIN_INGRESS, admit_proposal

    tenant_id = user.tenant_id

    # A machine principal, or nothing. Asked before the meter because a
    # non-machine caller HAS no agent identity to meter against -- there
    # is no per-agent bucket to charge, so there is nothing to place this
    # request in. Every principal past this line is an authenticated
    # agent with a bucket of its own.
    if not is_machine(user):
        raise HTTPException(
            status_code=403,
            detail=(
                "external submission is a machine-principal surface; a "
                "person proposes through the Console, not through ingress"
            ),
        )

    # A24.5: the agent the TOKEN names, never the one the path names. The
    # meter is charged to the authenticated principal, so naming another
    # agent in the path cannot move the cost onto that agent's bucket --
    # the impersonation refusal below is itself metered here.
    self_agent = user.user_id

    submissions = AgentSubmissionRepo(session)
    attempts = AgentIngressAttemptRepo(session)

    # -- serialize this agent's ingress (A24.11) -----------------------
    # Held for the rest of this transaction, covering the rate decision
    # AND the replay lookup->insert below. Without it, two callers with
    # one key both miss the lookup and the loser raises an integrity
    # error -- a 500 on precisely the retry the key exists to make safe,
    # reproduced on PostgreSQL by forcing that window.
    #
    # The only other lock in this path is the tenant admission lock taken
    # inside `admit_proposal`. The order is always agent then tenant, so
    # no cycle can form.
    await lock_agent_ingress(session, tenant_id, self_agent)

    # -- attempt rate (A24.13) -----------------------------------------
    # BEFORE the replay branch, deliberately: the first implementation
    # returned a replay first, which made replay an unmetered channel. A
    # replay stays functionally idempotent; it is not free.
    permitted, used = await admit_attempt(
        session, tenant_id=tenant_id, agent_id=self_agent,
    )
    if not permitted:
        # Refused without writing: a record that grew on every refusal
        # would amplify the traffic it exists to bound.
        await session.commit()
        raise HTTPException(
            status_code=429,
            detail=(
                f"this agent has made {used} ingress attempts in the last "
                f"{ATTEMPT_WINDOW_S // 60} minutes"
            ),
        )

    async def _settle(outcome: str):
        await attempts.record(
            tenant_id=tenant_id, agent_id=self_agent, outcome=outcome,
        )

    # -- authorization, now INSIDE the meter (A24.13) -------------------
    # Every refusal below is an authenticated request from a principal
    # with its own bucket, so each one costs that bucket exactly what an
    # accepted submission costs. The checks and their status codes are
    # unchanged; only where they sit relative to the meter has moved.
    #
    # Caught as a class rather than enumerated: a check added here later
    # is metered by construction instead of by whoever remembers.
    try:
        if not (
            "proposal.submit" in user.permissions or "*" in user.permissions
        ):
            raise HTTPException(
                status_code=403, detail="missing permission: proposal.submit",
            )
        if self_agent != agent_id:
            raise HTTPException(
                status_code=403,
                detail="a machine identity may submit for its own agent and no other",
            )
        # OBJECT_GATED, and not decoratively. For a machine principal the
        # caller IS the agent, so this asks whether the agent still
        # reaches its own scope rows -- the question A23-3's inert grants
        # make load-bearing: an agent whose grant was revoked, expired or
        # points at a deleted org unit reaches nothing and must not be
        # able to submit. Out of scope is 404, never 403.
        agent = await _require_visible_agent(session, tenant_id, agent_id, scope)
        if agent.status != STATUS_ACTIVE:
            raise HTTPException(
                status_code=409,
                detail=f"this agent is {agent.status or 'inactive'} and may not submit",
            )
        if getattr(agent, "paused_reason", ""):
            raise HTTPException(
                status_code=409,
                detail=f"this agent is paused: {agent.paused_reason}",
            )
    except HTTPException:
        await _settle(OUTCOME_REFUSED)
        await session.commit()
        raise

    # -- replay (A24.2) -----------------------------------------------
    # A retry must be cheap and must return the ORIGINAL answer.
    # `request_digest` is what separates a retry from a client bug --
    # same key, different work is 409, never silently answered with a
    # result that describes something else.
    digest = _submission_digest(body)
    prior = await submissions.find(tenant_id, agent_id, body.idempotency_key)
    if prior is not None:
        if prior.request_digest != digest:
            await _settle(OUTCOME_CONFLICT)
            await session.commit()
            raise HTTPException(
                status_code=409,
                detail=(
                    "this idempotency key was already used for a different "
                    "submission; use a new key for new work"
                ),
            )
        await _settle(OUTCOME_REPLAYED)
        await session.commit()
        response.status_code = 200
        return _submission_result(prior, replayed=True)

    # -- re-derivation (A24.3) ----------------------------------------
    # The ref is resolved by RE-RUNNING the same evaluation the dry-run
    # ran, not by looking anything up in a table. That is what makes a
    # stale reference structurally unable to succeed: the candidate has
    # to still be there, on the same condition, for the same component,
    # under the current contract, catalogue, capabilities and safety
    # state. No second reasoning path exists to disagree with.
    agent_scope = await load_agent_scope(
        session, tenant_id=tenant_id, agent_id=agent_id
    )
    devices = await FleetCacheRepo(session).list_all(tenant_id)
    catalogue_rows = await CapabilityCatalogueRepo(session).list_for_tenant(
        tenant_id
    )
    catalogue = {
        sub: candidates_for(catalogue_rows, sub)
        for sub in {r.subsystem for r in catalogue_rows}
    }
    prop_repo = AgentProposalRepo(session)
    midnight = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    repo = OperationalAgentRepo(session)
    would_propose = evaluate(
        catalogue=catalogue,
        agent=agent,
        scopes=await repo.list_scopes(agent_id),
        resolved_site_ids=agent_scope.site_ids,
        capabilities=await repo.list_capabilities(agent_id),
        devices=devices,
        incidents_by_device=await _incidents_by_device(session, tenant_id),
        autonomy_contract=await load_autonomy_contract(
            session,
            tenant_id=tenant_id,
            actor_id=attribution_key(agent_id, agent.version),
            actor_species="agent",
            permissions=AGENT_PERMISSIONS,
        ),
        attention_by_device={
            item["agent_id"]: item
            for item in (await load_attention(
                session, tenant_id=tenant_id, scope=agent_scope,
            ))["items"]
        },
        open_dedupe_keys=await prop_repo.all_dedupe_keys(tenant_id),
        proposals_today=await prop_repo.count_since(
            tenant_id, agent_id, midnight,
        ),
    )

    match = next(
        (
            p for p in would_propose
            if candidate_ref(tenant_id, p["dedupe_key"]) == body.candidate_ref
        ),
        None,
    )

    agent_version = agent.version

    if match is None:
        # Refused, and RECORDED. An agent whose submissions keep missing
        # is the signal an operator needs, and an unrecorded refusal
        # would also be a free request.
        row = await submissions.record(
            tenant_id=tenant_id, agent_id=agent_id,
            agent_version=agent_version,
            idempotency_key=body.idempotency_key, request_digest=digest,
            candidate_ref=body.candidate_ref, proposal_id=None,
            code="candidate_not_current",
            reason=(
                "this candidate is not among the actions the agent would "
                "propose right now; re-read the dry-run and submit a "
                "current candidate"
            ),
        )
        # A24.16: counted, not chained. A rejected candidate is an
        # ATTEMPT outcome, and appending every one of them to a
        # hash-chained governance store would be an amplification channel
        # against the platform's own integrity record. The submission row
        # above is the durable evidence an operator reads.
        await _settle(OUTCOME_REJECTED)
        await session.commit()
        response.status_code = 409
        return _submission_result(row, replayed=False)

    # -- admission (A24.6) --------------------------------------------
    # The ONE path, shared with the evaluator. It takes the tenant's
    # admission lock and re-checks committed dedupe keys, so two
    # concurrent submissions of the same candidate under DIFFERENT
    # idempotency keys cannot both create a proposal.
    proposal, code, reason = await admit_proposal(
        session,
        tenant_id=tenant_id,
        payload=match,
        origin=ORIGIN_INGRESS,
        actor_ref=agent_id,
        note=body.note,
    )
    row = await submissions.record(
        tenant_id=tenant_id, agent_id=agent_id, agent_version=agent_version,
        idempotency_key=body.idempotency_key, request_digest=digest,
        candidate_ref=body.candidate_ref,
        proposal_id=proposal.id if proposal is not None else None,
        code=code, reason=reason,
    )
    await _settle(OUTCOME_ACCEPTED if proposal is not None else OUTCOME_REJECTED)
    # A24.16: a submission that produced a proposal is a GOVERNED outcome
    # and stays on the chain. So does a governed refusal reached after
    # authorization -- there are at most a handful per agent per window,
    # bounded by the attempt limit above.
    await AuditRepo(session).append(
        actor=attribution_key(agent_id, agent_version),
        actor_ref=agent_id,
        action=(
            "agent_submission.accepted" if proposal is not None
            else "agent_submission.refused"
        ),
        subject=row.id,
        tenant_id=tenant_id,
        detail={
            "agent_id": agent_id,
            "proposal_id": proposal.id if proposal is not None else "",
            "action_type": match["action_type"],
            "device_agent_id": match["device_agent_id"],
            "code": code,
        },
    )
    await session.commit()
    if proposal is None:
        response.status_code = 409
    return _submission_result(row, replayed=False, proposal=proposal)


def _submission_digest(body: SubmitProposal) -> str:
    """What makes a retry a retry.

    Covers the fields that describe the WORK, and deliberately not
    `note`: re-sending the same candidate with a clarified note is the
    same submission, and 409-ing it would punish the honest case.
    """
    import hashlib

    payload = f"a6.v1|{body.candidate_ref}|{body.idempotency_key}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _submission_result(row, *, replayed: bool, proposal=None) -> dict:
    """One shape for every answer this route gives.

    `accepted` says a proposal EXISTS. It never says anything will run --
    the words matter, because the caller is a machine that will act on
    this field.
    """
    out = {
        "submission_id": row.id,
        "agent_id": row.agent_id,
        "proposal_id": row.proposal_id or "",
        "accepted": bool(row.proposal_id),
        "replayed": replayed,
        "code": row.code,
        "reason": row.reason,
        "submitted_at": row.created_at.isoformat() if row.created_at else None,
        "governs": (
            "A proposal was recorded. It confers nothing: it still faces "
            "the approval ledger, the dispatch gates, the Site Manager's "
            "safety state and the node's own allow list."
        ) if row.proposal_id else (
            "No proposal was created."
        ),
    }
    if proposal is not None:
        out["proposal"] = {
            "id": proposal.id,
            "status": proposal.status,
            "disposition": proposal.disposition,
            "disposition_reason": proposal.disposition_reason,
            "blocking_conditions": proposal.blocking_conditions or [],
            "action_type": proposal.action_type,
            "device_agent_id": proposal.device_agent_id,
            "site_id": proposal.site_id,
            "params": proposal.params,
            "requires_approval": proposal.status == PROPOSAL_AWAITING,
        }
    return out


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

    # A23-3 (lifecycle consistency): a retired agent holds no scope. Its
    # rows are revoked by timestamp -- history, not deletion -- so a
    # retired agent no longer pins an org unit against deletion and no
    # later reader can mistake it for an administered, reachable
    # principal. A paused agent keeps its rows: pausing is reversible.
    scopes_revoked = 0
    if target == STATUS_RETIRED:
        scopes_revoked = await repo.clear_scopes(agent.id, revoked_by=user.user_id)

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
                actor=actor, actor_ref=actor_of(user), action="agent_identity.retired",
                subject=agent.id, tenant_id=user.tenant_id,
                detail={"client_id": identity.keycloak_client_id,
                        "keycloak_disabled": not kc_reason,
                        "keycloak_detail": kc_reason or None},
            )

    await AuditRepo(session).append(
        actor=actor,
        actor_ref=actor_of(user),
        action=f"operational_agent.{transition}d",
        subject=agent.id,
        tenant_id=user.tenant_id,
        detail={
            "status": target,
            "version": agent.version,
            # A19.9: what was actually switched on, in the record itself.
            "activated_version": int(agent.activated_version or 0),
            # A23-3: a retired agent's scope rows are revoked with it.
            "scopes_revoked": scopes_revoked,
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

