"""S6 Campaigns API: governed capability orchestration across an estate.

Governance
----------
No new permission. `site.manage` configures a campaign (it is fleet
configuration), `action.approve` decides a site-wave, and `fleet.view`
reads — the same split A0's Operational Agents use, and the permission
vocabulary stays fixed (spec §4).

Approval granularity is **per site-wave for every action requiring a
human** (D1). This router creates no campaign-level approval semantics:
each wave becomes a subject on the existing E0.1 ledger under
`subject_type = campaign_wave`, decided by the same function, policy and
duplicate guarantee a node action gets. A Console that offers batch
review is offering a review affordance, not a merged decision.

Scope
-----
E1.2 throughout: the target read is scope-filtered, the delegation
ceiling caps what a campaign may reach, and a campaign is visible only
to a principal who can see at least one site it touches.

Authority
---------
Creating or approving a campaign authorizes nothing by itself. Every
dispatched action still passes the node's unchanged funnel — allow list,
preconditions, lease, blast radius — and the node may refuse.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq.capabilities import action_facts

from harkeniq_cc.api.deps import (
    forbid_out_of_scope,
    get_scope,
    get_session,
    require_permission,
)
from harkeniq_cc.actor import actor_of
from harkeniq_cc.auth import UserContext
from harkeniq_cc.autonomy import AUTONOMOUS, DENIED
from harkeniq_cc.campaign_runner import (
    acknowledge as run_acknowledge,
    campaign_actor,
    preflight as run_preflight,
)
from harkeniq_cc.campaign_runner import advance_campaign, build_waves
from harkeniq_cc.campaigns import (
    DISPATCHABLE,
    EDITABLE_STATUSES,
    NEEDS_ACKNOWLEDGEMENT,
    STATUS_AWAITING_APPROVAL,
    STATUS_CANCELLED,
    STATUS_DRAFT,
    STATUS_RUNNING,
    TERMINAL_STATUSES,
    acknowledgement_valid,
    campaign_progress,
    can_seek_approval,
)
from harkeniq_cc.db.repos import AuditRepo, CampaignRepo, OrgUnitRepo, SiteRepo
from harkeniq_cc.api.operational_agents import _scope_rule_within
from harkeniq_cc.governance import load_autonomy_contract
from harkeniq_cc.operational_agent import (
    SCOPE_DEVICE,
    SCOPE_DEVICE_CLASS,
    SCOPE_ORG_UNIT,
    SCOPE_SITE,
)
from harkeniq_cc.scope import SCOPE_DEVICE, SCOPE_DEVICE_CLASS, expand_rules_to_site_ids

logger = logging.getLogger("harkeniq.cc.api.campaigns")

router = APIRouter(prefix="/api/campaigns", tags=["campaigns"])

SCOPE_TYPES = (SCOPE_ORG_UNIT, SCOPE_SITE, SCOPE_DEVICE_CLASS, SCOPE_DEVICE)


class ScopeRule(BaseModel):
    scope_type: str
    scope_ref: str = Field(..., min_length=1, max_length=128)


class CampaignBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str = Field("", max_length=1024)
    action_type: str = Field(..., min_length=1, max_length=64)
    params: dict = Field(default_factory=dict)
    scopes: list[ScopeRule] = Field(default_factory=list)
    site_concurrency: int = Field(1, ge=1, le=50)
    max_wave_size: int = Field(5, ge=1, le=100)


class AcknowledgeBody(BaseModel):
    exclude: list[str] = Field(default_factory=list)
    #: A deliberate speed bump. Acknowledging that part of the estate
    #: will refuse is a decision, and a decision made by clicking
    #: "confirm" on a dialog nobody read is not one.
    confirm: bool = False


def _campaign_dict(c, sites=(), targets=()) -> dict:
    sites, targets = list(sites), list(targets)
    return {
        "id": c.id,
        "name": c.name,
        "description": c.description,
        "action_type": c.action_type,
        "params": c.params or {},
        "status": c.status,
        "version": c.version,
        "actor": campaign_actor(c.id, c.version),
        "site_concurrency": c.site_concurrency,
        "max_wave_size": c.max_wave_size,
        "created_by": c.created_by,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "preflight_at": c.preflight_at.isoformat() if c.preflight_at else None,
        "acknowledged_by": c.acknowledged_by or None,
        "acknowledged_at": (
            c.acknowledged_at.isoformat() if c.acknowledged_at else None
        ),
        "acknowledgement_valid": acknowledgement_valid(c),
        "halt_reason": c.halt_reason or None,
        "completed_at": c.completed_at.isoformat() if c.completed_at else None,
        "progress": campaign_progress(sites, targets),
    }


def _target_dict(t) -> dict:
    return {
        "device_agent_id": t.device_agent_id,
        "device_name": t.device_name,
        "device_class": t.device_class,
        "site_id": t.site_id,
        "applicability": t.applicability,
        "reason": t.reason or None,
        "status": t.status,
        "revalidation": t.revalidation or None,
        "revalidation_reason": t.revalidation_reason or None,
        "outcome": t.outcome or None,
        "error": t.error or None,
    }


def _site_dict(s) -> dict:
    return {
        "site_id": s.site_id,
        "site_name": s.site_name,
        "status": s.status,
        "order_index": s.order_index,
        "current_wave": s.current_wave,
        "wave_count": s.wave_count,
        "halt_reason": s.halt_reason or None,
    }


async def _validate(session, tenant_id: str, body: CampaignBody) -> None:
    """A campaign may only name a governed class and real scope refs."""
    facts = action_facts()
    fact = facts.get(body.action_type.upper())
    if fact is None:
        raise HTTPException(
            400, f"{body.action_type!r} is not a governed action class"
        )
    if not fact["implemented"]:
        # The Capability Registry's platform-level truth. Refused here
        # rather than discovered at dispatch, which is the whole product
        # requirement: no executor in this build can perform it, so no
        # scope and no allow-list change could make this campaign run.
        raise HTTPException(
            400,
            f"{body.action_type.upper()} is a governed action class that no "
            f"executor in this platform implements. A campaign for it could "
            f"never run on any device; implementing it is a separate "
            f"governed capability slice.",
        )
    if not body.scopes:
        raise HTTPException(
            400, "a campaign with no scope rows would target no devices"
        )
    site_ids = {s.id for s in await SiteRepo(session).list_all(tenant_id)}
    unit_ids = {u.id for u in await OrgUnitRepo(session).list_all(tenant_id)}
    for rule in body.scopes:
        if rule.scope_type not in SCOPE_TYPES:
            raise HTTPException(400, f"scope_type must be one of {list(SCOPE_TYPES)}")
        if rule.scope_type == SCOPE_ORG_UNIT and rule.scope_ref not in unit_ids:
            raise HTTPException(
                400, f"org unit {rule.scope_ref!r} does not exist in this tenant"
            )
        if rule.scope_type == SCOPE_SITE and rule.scope_ref not in site_ids:
            raise HTTPException(
                400, f"site {rule.scope_ref!r} is not registered to this tenant"
            )
        if rule.scope_type == SCOPE_DEVICE_CLASS and rule.scope_ref.lower() not in (
            "server", "switch",
        ):
            raise HTTPException(400, "device_class must be 'server' or 'switch'")


def _enforce_ceiling(creator_scope, scopes) -> None:
    """A campaign may never reach further than the person who built it.

    The E1.2 delegation ceiling, and it reuses the Operational Agent's
    `_scope_rule_within` rather than restating it. That matters for more
    than tidiness: an org-unit rule has to be resolved to its
    MATERIALIZED PATH before `permits` can answer, and `device_class`
    spans the whole fleet so only a tenant-wide principal may delegate
    one. A second implementation would have got both subtly wrong --
    the first cut here passed `org_unit_id=` to a function that takes
    `org_unit_path=`, which unit tests using only site scope never
    reached and the live stack found immediately.
    """
    for rule in scopes:
        if not _scope_rule_within(creator_scope, rule):
            raise HTTPException(
                status_code=403,
                detail=(
                    f"cannot target {rule.scope_type} {rule.scope_ref!r}: it "
                    f"is outside your own authorized scope, and a campaign "
                    f"may never reach further than the person who created it"
                ),
            )


async def _scope_rules(campaign_id: str, body_scopes) -> list:
    from types import SimpleNamespace

    return [
        SimpleNamespace(scope_type=s.scope_type, scope_ref=s.scope_ref)
        for s in body_scopes
    ]


async def _rule_reach(session, tenant_id: str, rules) -> frozenset[str]:
    """The sites a campaign's RULES reach, through the org tree (A23.3).

    This is the campaign's own reach and knows nothing about the caller.
    Device and device_class rules have no site of their own; they are
    resolved against the fleet at preflight, and for visibility they are
    treated as tenant-spanning (only a tenant-wide reader sees them).
    """
    units = await OrgUnitRepo(session).list_all(tenant_id)
    sites = await SiteRepo(session).list_all(tenant_id)
    return expand_rules_to_site_ids(rules, units, sites)


def _visible_sites(scope):
    if getattr(scope, "tenant_wide", False):
        return None
    return set(getattr(scope, "site_ids", ()) or ())


async def _campaign_visible(session, tenant_id: str, scope, rules, site_rows) -> bool:
    """May this caller READ this campaign? (A23, READ_SCOPED made true.)

    Visible when the caller can see at least one site the campaign
    reaches. Before preflight there are no site rows, so the reach is
    derived from the scope rules through the tree -- the old check
    treated "no site rows" as "visible to everyone", which made every
    draft campaign in the tenant readable by any site-scoped principal.
    """
    visible = _visible_sites(scope)
    if visible is None:
        return True
    reach = {s.site_id for s in site_rows}
    if not reach:
        reach = set(await _rule_reach(session, tenant_id, rules))
        if any(r.scope_type in (SCOPE_DEVICE, SCOPE_DEVICE_CLASS) for r in rules):
            return False
    return bool(reach & visible)


def _only_visible(scope, rows, key=lambda r: r.site_id):
    """Narrow site-anchored campaign rows to the caller's sites."""
    visible = _visible_sites(scope)
    if visible is None:
        return list(rows)
    return [r for r in rows if key(r) in visible]


async def _require_operable(session, tenant_id: str, scope, campaign_id: str):
    """The campaign, gated for a LIFECYCLE mutation (A23.3).

    Acknowledge, submit, cancel and advance change what will execute, so
    they sit under the same ceiling as creation and preflight: the
    caller must hold `site.manage` over EVERY scope rule the campaign
    names. An invisible campaign is 404 first, so the gate never
    confirms one the caller cannot see.
    """
    repo = CampaignRepo(session)
    campaign = await repo.get(tenant_id, campaign_id)
    if campaign is None:
        raise HTTPException(404, "campaign not found")
    rules = list(await repo.scopes(campaign_id))
    if not await _campaign_visible(
        session, tenant_id, scope, rules, await repo.sites(campaign_id)
    ):
        raise HTTPException(404, "campaign not found")
    _enforce_ceiling(scope, rules)
    return repo, campaign


async def _require_readable(session, tenant_id: str, scope, campaign_id: str):
    """The campaign, or 404 if it does not exist or the caller cannot see it."""
    repo = CampaignRepo(session)
    campaign = await repo.get(tenant_id, campaign_id)
    if campaign is None:
        raise HTTPException(404, "campaign not found")
    if not await _campaign_visible(
        session, tenant_id, scope, list(await repo.scopes(campaign_id)),
        await repo.sites(campaign_id),
    ):
        raise HTTPException(404, "campaign not found")
    return repo, campaign


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


@router.get("/", dependencies=[Depends(require_permission("fleet.view"))])
async def list_campaigns(
    status: Optional[str] = Query(None),
    user: UserContext = Depends(require_permission("fleet.view")),
    session: AsyncSession = Depends(get_session),
    scope=Depends(get_scope),
) -> dict:
    repo = CampaignRepo(session)
    # A23: visibility is derived per campaign so a DRAFT (no site rows
    # yet) is judged by its scope rules, not shown to everyone.
    rows = [
        c for c in await repo.list_all(user.tenant_id, status=status)
        if await _campaign_visible(
            session, user.tenant_id, scope,
            list(await repo.scopes(c.id)), await repo.sites(c.id),
        )
    ]
    out = []
    for c in rows:
        out.append(_campaign_dict(
            c, await repo.sites(c.id), await repo.targets(c.id),
        ))
    return {"campaigns": out, "total": len(out)}


@router.get("/{campaign_id}", dependencies=[Depends(require_permission("fleet.view"))])
async def get_campaign(
    campaign_id: str,
    user: UserContext = Depends(require_permission("fleet.view")),
    session: AsyncSession = Depends(get_session),
    scope=Depends(get_scope),
) -> dict:
    repo = CampaignRepo(session)
    campaign = await repo.get(user.tenant_id, campaign_id)
    if campaign is None:
        raise HTTPException(404, "campaign not found")
    sites = await repo.sites(campaign_id)
    # E1.2 layer 2: 404 rather than 403 -- a 403 confirms it exists.
    if not await _campaign_visible(
        session, user.tenant_id, scope, list(await repo.scopes(campaign_id)), sites
    ):
        raise HTTPException(404, "campaign not found")
    targets = await repo.targets(campaign_id)
    # A23: the aggregate is computed over the whole campaign (the status
    # is one fact); the per-site and per-device ROWS are the caller's.
    detail = _campaign_dict(campaign, sites, targets)
    detail["sites"] = [_site_dict(s) for s in _only_visible(scope, sites)]
    detail["targets"] = [_target_dict(t) for t in _only_visible(scope, targets)]
    return detail


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


@router.post("/", status_code=201,
             dependencies=[Depends(require_permission("site.manage"))])
async def create_campaign(
    body: CampaignBody,
    user: UserContext = Depends(require_permission("site.manage")),
    session: AsyncSession = Depends(get_session),
    scope=Depends(get_scope),
) -> dict:
    """Create a campaign in `draft`. It targets nothing until preflight."""
    _enforce_ceiling(scope, body.scopes)
    await _validate(session, user.tenant_id, body)
    repo = CampaignRepo(session)
    actor = user.email or user.user_id
    campaign = await repo.create(
        tenant_id=user.tenant_id,
        name=body.name,
        description=body.description,
        action_type=body.action_type.upper(),
        params=dict(body.params or {}),
        site_concurrency=body.site_concurrency,
        max_wave_size=body.max_wave_size,
        created_by=actor,
    )
    # Selection lives in its own table, NOT in `params`. `params` is the
    # action's payload and is sent verbatim to the node; scope rows in
    # there would ship governance metadata to every device as execution
    # parameters.
    await repo.replace_scopes(
        campaign.id, [(s.scope_type, s.scope_ref) for s in body.scopes]
    )
    await AuditRepo(session).append(
        actor=actor,
        actor_ref=actor_of(user),
        action="campaign.created",
        subject=campaign.id,
        tenant_id=user.tenant_id,
        detail={
            "name": campaign.name,
            "action_type": campaign.action_type,
            "scopes": [[s.scope_type, s.scope_ref] for s in body.scopes],
        },
    )
    await session.commit()
    return _campaign_dict(campaign)


@router.post("/{campaign_id}/preflight",
             dependencies=[Depends(require_permission("site.manage"))])
async def preflight_campaign(
    campaign_id: str,
    request: Request,
    user: UserContext = Depends(require_permission("site.manage")),
    session: AsyncSession = Depends(get_session),
    scope=Depends(get_scope),
) -> dict:
    """Resolve targets and ask the Capability Registry about every one.

    Mandatory before approval. An approver must never be the first
    person to learn that part of the estate cannot run the action.
    """
    repo = CampaignRepo(session)
    campaign = await repo.get(user.tenant_id, campaign_id)
    if campaign is None:
        raise HTTPException(404, "campaign not found")
    if campaign.status not in EDITABLE_STATUSES:
        raise HTTPException(
            409, f"a campaign in status {campaign.status!r} cannot be re-preflighted"
        )
    rules = list(await repo.scopes(campaign_id))
    if not await _campaign_visible(
        session, user.tenant_id, scope, rules, await repo.sites(campaign_id)
    ):
        raise HTTPException(404, "campaign not found")
    _enforce_ceiling(scope, rules)
    # A23.3: the target set is the campaign's OWN rules, expanded through
    # the org tree, intersected with the caller's effective scope. This
    # used to resolve the caller a second time -- with
    # role_permissions=["*"] and realm="" -- and union THAT into the
    # target set, so a one-site campaign preflighted by a tenant owner
    # targeted the whole estate. Caller authority may constrain; it may
    # never enlarge.
    summary = await run_preflight(
        session,
        request.app.state.cc,
        tenant_id=user.tenant_id,
        campaign=campaign,
        scope_rules=rules,
        resolved_site_ids=await _rule_reach(session, user.tenant_id, rules),
        caller_scope=scope,
        actor=user.email or user.user_id, actor_ref=actor_of(user),
    )
    await session.commit()
    targets = await repo.targets(campaign_id)
    return {
        "campaign_id": campaign_id,
        "status": campaign.status,
        "summary": summary,
        "requires_acknowledgement": summary["warn_not_permitted"] + summary["unknown"],
        "targets": [_target_dict(t) for t in targets],
    }


@router.post("/{campaign_id}/acknowledge",
             dependencies=[Depends(require_permission("site.manage"))])
async def acknowledge_campaign(
    campaign_id: str,
    body: AcknowledgeBody,
    user: UserContext = Depends(require_permission("site.manage")),
    session: AsyncSession = Depends(get_session),
    scope=Depends(get_scope),
) -> dict:
    """A named human excludes or accepts every warned/unknown target (D2)."""
    repo, campaign = await _require_operable(session, user.tenant_id, scope, campaign_id)
    if campaign.status not in EDITABLE_STATUSES:
        raise HTTPException(
            409, f"a campaign in status {campaign.status!r} cannot be acknowledged"
        )
    if not campaign.preflight_at:
        raise HTTPException(409, "preflight this campaign before acknowledging it")
    targets = await repo.targets(campaign_id)
    warned = [
        t for t in targets
        if t.applicability in NEEDS_ACKNOWLEDGEMENT
        and t.device_agent_id not in set(body.exclude or [])
    ]
    if warned and not body.confirm:
        raise HTTPException(
            400,
            f"{len(warned)} target(s) are implemented but not currently "
            f"permitted by their node, or have not declared. Set confirm=true "
            f"to accept that they may be refused at execution time, or list "
            f"them in `exclude`.",
        )
    result = await run_acknowledge(
        session,
        tenant_id=user.tenant_id,
        campaign=campaign,
        exclude_device_ids=body.exclude or [],
        actor=user.email or user.user_id, actor_ref=actor_of(user),
    )
    await session.commit()
    return {"campaign_id": campaign_id, "status": campaign.status, **result}


@router.post("/{campaign_id}/submit",
             dependencies=[Depends(require_permission("site.manage"))])
async def submit_campaign(
    campaign_id: str,
    user: UserContext = Depends(require_permission("site.manage")),
    session: AsyncSession = Depends(get_session),
    scope=Depends(get_scope),
) -> dict:
    """Move a preflighted, acknowledged campaign into the approval flow.

    The autonomy contract decides what happens next: a class the tenant
    runs autonomously needs no human, one that requires approval raises
    a site-wave decision per wave, and a denied class cannot proceed.
    """
    repo, campaign = await _require_operable(session, user.tenant_id, scope, campaign_id)
    targets = await repo.targets(campaign_id)
    ok, reason = can_seek_approval(campaign, targets)
    if not ok:
        raise HTTPException(409, reason)

    contract = await load_autonomy_contract(
        session,
        tenant_id=user.tenant_id,
        actor_id=campaign_actor(campaign.id, campaign.version),
        actor_species="campaign",
        permissions=list(user.permissions),
    )
    row = next(
        (c for c in contract["action_classes"]
         if c["action_type"] == campaign.action_type), None,
    )
    disposition = (row or {}).get("disposition", DENIED)
    if disposition == DENIED:
        raise HTTPException(
            409,
            f"{campaign.action_type} is denied for this tenant right now: "
            f"{(row or {}).get('disposition_reason', 'no autonomy contract row')}",
        )
    # Q1: every site-wave of this campaign version becomes a subject NOW,
    # so the complete set of decisions is known before execution begins.
    # An autonomous class raises none: there is no human decision to
    # record, and manufacturing one would imply a review nobody did.
    autonomous = disposition == AUTONOMOUS
    built = await build_waves(
        session, tenant_id=user.tenant_id, campaign=campaign,
        autonomous=autonomous,
    )
    if built["waves"] == 0:
        raise HTTPException(
            409,
            "no site returned a wave plan, so there is nothing to approve "
            "or run; re-preflight this campaign",
        )
    campaign.status = STATUS_RUNNING if autonomous else STATUS_AWAITING_APPROVAL
    await AuditRepo(session).append(
        actor=user.email or user.user_id, actor_ref=actor_of(user),
        action="campaign.submitted",
        subject=campaign.id,
        tenant_id=user.tenant_id,
        detail={"disposition": disposition, "version": campaign.version, **built},
    )
    await session.commit()
    return {
        "campaign_id": campaign_id,
        "status": campaign.status,
        "disposition": disposition,
        "requires_human_approval": not autonomous,
        # Approval is per site-wave, universally (D1). This endpoint is a
        # TRANSITION into the existing /api/approvals workflow -- it makes
        # no decision and stores no approver.
        "approval_granularity": "site_wave",
        "approval_surface": "/api/approvals",
        **built,
    }


@router.post("/{campaign_id}/cancel",
             dependencies=[Depends(require_permission("site.manage"))])
async def cancel_campaign(
    campaign_id: str,
    user: UserContext = Depends(require_permission("site.manage")),
    session: AsyncSession = Depends(get_session),
    scope=Depends(get_scope),
) -> dict:
    repo, campaign = await _require_operable(session, user.tenant_id, scope, campaign_id)
    if campaign.status in TERMINAL_STATUSES:
        raise HTTPException(409, f"campaign already {campaign.status}")
    campaign.status = STATUS_CANCELLED
    await AuditRepo(session).append(
        actor=user.email or user.user_id, actor_ref=actor_of(user),
        action="campaign.cancelled",
        subject=campaign.id,
        tenant_id=user.tenant_id,
        detail={"version": campaign.version},
    )
    await session.commit()
    return {"campaign_id": campaign_id, "status": campaign.status}


@router.post("/{campaign_id}/advance",
             dependencies=[Depends(require_permission("site.manage"))])
async def advance_campaign_endpoint(
    campaign_id: str,
    request: Request,
    user: UserContext = Depends(require_permission("site.manage")),
    session: AsyncSession = Depends(get_session),
    scope=Depends(get_scope),
) -> dict:
    """Move every eligible site forward by at most one wave.

    The explicit operational control surface (Q2). The durable CC runner
    calls exactly the same function on its own schedule, so an operator
    pressing this and the loop firing cannot diverge — and neither can
    double-execute, because the dispatch ledger's composite key makes a
    repeat physically unable to exist.
    """
    repo, campaign = await _require_operable(session, user.tenant_id, scope, campaign_id)
    if campaign.status not in (STATUS_RUNNING, STATUS_AWAITING_APPROVAL):
        raise HTTPException(
            409, f"a campaign in status {campaign.status!r} cannot advance"
        )
    if campaign.status == STATUS_AWAITING_APPROVAL:
        campaign.status = STATUS_RUNNING
    result = await advance_campaign(
        session, request.app.state.cc,
        tenant_id=user.tenant_id, campaign=campaign,
    )
    await session.commit()
    return {"campaign_id": campaign_id, "status": campaign.status, **result}


@router.get("/{campaign_id}/waves",
            dependencies=[Depends(require_permission("fleet.view"))])
async def campaign_waves(
    campaign_id: str,
    user: UserContext = Depends(require_permission("fleet.view")),
    session: AsyncSession = Depends(get_session),
    scope=Depends(get_scope),
) -> dict:
    """The site-waves, each with the exact devices it authorizes.

    This is what an approver reads before deciding: not "site A, wave 1"
    but the named devices that wave will act on, and the plan hash the
    decision binds to.
    """
    repo, campaign = await _require_readable(session, user.tenant_id, scope, campaign_id)
    return {
        "campaign_id": campaign_id,
        "approval_granularity": "site_wave",
        "waves": [
            {
                "site_id": w.site_id,
                "wave_index": w.wave_index,
                "device_agent_ids": list(w.device_agent_ids or []),
                "domain_span": w.domain_span,
                "plan_hash": w.plan_hash,
                "subject_ref": w.subject_ref or None,
                "status": w.status,
                "void_reason": w.void_reason or None,
                "decided_by": w.decided_by or None,
            }
            for w in _only_visible(scope, await repo.waves(campaign_id))
        ],
    }


@router.get("/{campaign_id}/targets",
            dependencies=[Depends(require_permission("fleet.view"))])
async def campaign_targets(
    campaign_id: str,
    user: UserContext = Depends(require_permission("fleet.view")),
    session: AsyncSession = Depends(get_session),
    scope=Depends(get_scope),
) -> dict:
    repo, campaign = await _require_readable(session, user.tenant_id, scope, campaign_id)
    return {
        "campaign_id": campaign_id,
        "targets": [
            _target_dict(t)
            for t in _only_visible(scope, await repo.targets(campaign_id))
        ],
    }


@router.get("/{campaign_id}/sites",
            dependencies=[Depends(require_permission("fleet.view"))])
async def campaign_sites(
    campaign_id: str,
    user: UserContext = Depends(require_permission("fleet.view")),
    session: AsyncSession = Depends(get_session),
    scope=Depends(get_scope),
) -> dict:
    repo, campaign = await _require_readable(session, user.tenant_id, scope, campaign_id)
    return {
        "campaign_id": campaign_id,
        "sites": [
            _site_dict(s) for s in _only_visible(scope, await repo.sites(campaign_id))
        ],
    }
