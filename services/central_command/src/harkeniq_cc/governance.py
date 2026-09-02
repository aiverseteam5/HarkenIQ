"""One loader for the autonomy contract, shared by every consumer.

A1 (2026-08-30). `GET /api/autonomy` and the Operational Agent evaluator
must read the SAME contract from the SAME inputs. If each assembled its
own repository reads they would drift the first time an input was added,
and the operator would be looking at a different governance state than
the agent acted on. That failure would be invisible until it mattered,
which is the worst shape a governance bug can take.

So the fetch lives here once. The composition still lives in
`harkeniq_cc.autonomy` and stays pure.
"""

from __future__ import annotations

from typing import Iterable, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_cc.autonomy import build_autonomy
from harkeniq_cc.capabilities import build_capability_registry
from harkeniq_cc.db.repos import (
    ApprovalPolicyRepo,
    AutonomyBudgetRepo,
    FleetCacheRepo,
    LearnedSignalRepo,
    OrgUnitRepo,
    OutcomeHistoryRepo,
    SafetyStateRepo,
    ScopeGrantRepo,
    SiteRepo,
    StopSwitchRepo,
    TenantSettingsRepo,
)
from harkeniq_cc.scope import (
    PRINCIPAL_AGENT,
    PRINCIPAL_USER,
    SCOPE_ONLY_MARKER,
    ResolvedScope,
    resolve,
)


async def load_scope(
    session: AsyncSession,
    *,
    tenant_id: str,
    principal_ref: str,
    role_permissions,
    principal_type: str = PRINCIPAL_USER,
    realm: str = "",
) -> ResolvedScope:
    """Resolve one principal's authorization scope. E1.2.

    The ONE loader for scope, for the same reason `load_autonomy_contract`
    is the one loader for the contract: a human and an agent that
    assembled their own inputs would drift, and a scope that drifts is an
    authorization bug rather than a display bug.

    Humans and Operational Agents differ only in `principal_type`. The
    rows, the tree, the enforcement mode and the resolver are identical.
    """
    from harkeniq_cc.grant_integrity import role_ceiling_for

    grants = await ScopeGrantRepo(session).list_for_principal(
        tenant_id, principal_ref, principal_type=principal_type,
        realm=realm,
    )
    org_units = await OrgUnitRepo(session).list_all(tenant_id)
    sites = await SiteRepo(session).list_all(tenant_id)
    enforcement = await TenantSettingsRepo(session).enforcement(tenant_id)
    return resolve(
        tenant_id=tenant_id,
        principal_type=principal_type,
        principal_ref=principal_ref,
        role_permissions=role_permissions,
        grant_rows=grants,
        org_units=org_units,
        sites=sites,
        enforcement=enforcement,
        # A23-3: the role a grant RECORDS is a ceiling the grantor
        # asserted. Supplied here so the resolver stays ignorant of role
        # names and every caller of this loader narrows identically.
        role_ceiling_for=role_ceiling_for,
    )


async def load_agent_scope(
    session: AsyncSession, *, tenant_id: str, agent_id: str
) -> ResolvedScope:
    """An Operational Agent's scope, through the same resolver.

    WHERE, never WHETHER (A22.13). This used to resolve with
    ``role_permissions=["*"]`` and justify it: an agent's authority is its
    A0 bindings plus the autonomy contract, and *it does not call the HTTP
    API, the CC-resident evaluator does*. A3 removed that premise -- an
    agent holds a credential now -- and resolved that way the scope
    answered ``permits("action.approve")`` with True. It could have
    approved its own proposals. It was latent only because all four call
    sites read `.site_ids`.

    `SCOPE_ONLY_MARKER` keeps the grant arithmetic identical, so no agent
    loses reach, while making a permission question on this scope an
    error instead of a yes.
    """
    return await load_scope(
        session,
        tenant_id=tenant_id,
        principal_ref=agent_id,
        role_permissions=[SCOPE_ONLY_MARKER],
        principal_type=PRINCIPAL_AGENT,
        # An agent is not a realm principal: its id is a CC row id, so
        # its grants carry no realm and are never narrowed by one.
        realm="",
    )


async def load_autonomy_contract(
    session: AsyncSession,
    *,
    tenant_id: str,
    actor_id: str,
    actor_species: str,
    permissions: Iterable[str],
    site_id: Optional[str] = None,
    action_type: Optional[str] = None,
) -> dict:
    """Fetch every tenant-scoped input and compose the contract.

    Every read below is tenant-scoped by its repository; `site_id`
    narrows within the tenant and can never widen beyond it.
    """
    budgets = await AutonomyBudgetRepo(session).list_all(tenant_id)
    stop_switch = await StopSwitchRepo(session).get(tenant_id)
    outcomes = await OutcomeHistoryRepo(session).list_outcome_dicts(tenant_id)
    safety_rows = await SafetyStateRepo(session).list_for_tenant(tenant_id)
    sites = await SiteRepo(session).list_all(tenant_id)
    learned = await LearnedSignalRepo(session).list_active(tenant_id)
    # Approval policies shape what "requires approval" actually means for
    # a class. Read at fleet.view even though managing them needs
    # site.manage: knowing an action needs two approvers is posture, and
    # the posture read-split (D2) is the whole point of this surface.
    policies = await ApprovalPolicyRepo(session).list_all(tenant_id)

    return build_autonomy(
        tenant_id=tenant_id,
        actor_id=actor_id,
        actor_species=actor_species,
        permissions=permissions,
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


async def load_capability_registry(
    session: AsyncSession,
    *,
    tenant_id: str,
    scope=None,
    site_id: Optional[str] = None,
    action_type: Optional[str] = None,
) -> dict:
    """Fetch the caller's visible fleet and compose the Registry.

    Same discipline as `load_autonomy_contract`, and for the same
    reason: the Console and the Operational Agent must read capability
    truth from ONE loader over ONE set of reads. If the page and the
    agent could see different capability sets, an operator would approve
    a proposal the agent should never have made and neither surface
    could explain why.

    `scope` is the E1.2 resolved scope. It is passed into the repository
    read, so what the Registry describes is exactly the fleet this
    principal may see -- never more, and never the whole tenant as a
    convenience.
    """
    devices = await FleetCacheRepo(session).list_all(tenant_id, scope=scope)
    sites = await SiteRepo(session).list_all(tenant_id, scope=scope)
    return build_capability_registry(
        tenant_id=tenant_id,
        devices=devices,
        sites=sites,
        site_id=site_id,
        action_type=action_type,
    )


async def load_attention(
    session: AsyncSession,
    *,
    tenant_id: str,
    site_id: Optional[str] = None,
    scope=None,
    band: Optional[str] = None,
    limit: Optional[int] = None,
) -> dict:
    """Fetch every input and compose the attention answer. ONE of these.

    `/api/attention` and the Operational Agent evaluator must rank the
    same devices from the same evidence, or the agent acts on a picture
    the operator has never seen. That was written down here and then NOT
    done: the router carried a near-verbatim copy whose `band` filter ran
    BEFORE ranking, so filtering reordered `rank` -- and rank is what
    decides which devices consume an agent's proposal budget. A5 (A22.11)
    makes this the only implementation.

    `scope` is E1.2's resolver (A22.9). It was missing entirely, so a
    site-scoped principal read every site's attention state -- and this is
    the ONE read every Operational Agent is required to hold. Both callers
    now supply the caller's own scope, which is also what makes "HTTP and
    in-process rank identically" true for a given principal rather than
    only for a tenant-wide one.

    `band` is a PURE FILTER applied AFTER ranking, which is what the
    endpoint's own contract always claimed: rank 1 means first in the
    principal's scope, never first on the page.
    """
    from harkeniq_cc.attention import build_attention
    from harkeniq_cc.db.repos import (
        ApprovalRouteRepo,
        CveFeedRepo,
        FleetCacheRepo,
        FleetPatternRepo,
        IncidentRepo,
        WarrantyRepo,
    )
    from harkeniq_cc.exposure import match_exposures
    from harkeniq_cc.predictive import cohort_failure_rates, score_device
    from harkeniq_cc.warranty.base import warranty_status

    devices = await FleetCacheRepo(session).list_all(tenant_id, scope=scope)
    if site_id:
        devices = [d for d in devices if d.site_id == site_id]
    outcomes = await OutcomeHistoryRepo(session).list_device_outcome_dicts(tenant_id)
    warranty_map = await WarrantyRepo(session).get_map(
        [d.service_tag for d in devices], tenant_id=tenant_id,
    )
    cve_entries = await CveFeedRepo(session).list_all(tenant_id=tenant_id)
    pending_routes = await ApprovalRouteRepo(session).list_pending(
        tenant_id, scope=scope,
    )
    patterns = await FleetPatternRepo(session).list_patterns(tenant_id=tenant_id)
    sites = await SiteRepo(session).list_all(tenant_id, scope=scope)
    learned = await LearnedSignalRepo(session).list_active(tenant_id)
    open_incidents = await IncidentRepo(session).list_incidents(
        tenant_id, status="open", site_id=site_id, limit=1000, scope=scope,
    )

    cohorts = cohort_failure_rates(outcomes)
    by_device: dict[str, list[dict]] = {}
    for oc in outcomes:
        by_device.setdefault(oc["device_agent_id"], []).append(oc)

    risks = []
    for dev in devices:
        warranty = warranty_map.get(dev.service_tag)
        risk = score_device(
            agent_id=dev.agent_id,
            outcomes=by_device.get(dev.agent_id, []),
            cohort_failure_rate=cohorts.get((dev.vendor, dev.model)),
            health=dev.health,
            warranty_status=warranty_status(warranty.end_date) if warranty else "",
            vendor=dev.vendor,
            model=dev.model,
        )
        risk.site_id = dev.site_id
        risk.agent_name = dev.agent_name
        risks.append(risk)

    result = build_attention(
        devices=devices,
        risks=risks,
        exposures=match_exposures(devices, cve_entries),
        warranty_map=warranty_map,
        pending_routes=pending_routes,
        patterns=patterns,
        sites=sites,
        tenant_id=tenant_id,
        learned_signals=learned,
        incidents=open_incidents,
    )
    # AFTER ranking, never before. Rank is assigned over the principal's
    # whole scope, so "rank 1" always means first in that scope.
    if band:
        result["items"] = [i for i in result["items"] if i.get("band") == band]
    if limit is not None:
        result["items"] = result["items"][:limit]
    result["returned"] = len(result["items"])
    return result
