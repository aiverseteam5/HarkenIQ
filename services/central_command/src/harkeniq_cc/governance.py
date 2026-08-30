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
from harkeniq_cc.db.repos import (
    ApprovalPolicyRepo,
    AutonomyBudgetRepo,
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
) -> ResolvedScope:
    """Resolve one principal's authorization scope. E1.2.

    The ONE loader for scope, for the same reason `load_autonomy_contract`
    is the one loader for the contract: a human and an agent that
    assembled their own inputs would drift, and a scope that drifts is an
    authorization bug rather than a display bug.

    Humans and Operational Agents differ only in `principal_type`. The
    rows, the tree, the enforcement mode and the resolver are identical.
    """
    grants = await ScopeGrantRepo(session).list_for_principal(
        tenant_id, principal_ref, principal_type=principal_type
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
    )


async def load_agent_scope(
    session: AsyncSession, *, tenant_id: str, agent_id: str
) -> ResolvedScope:
    """An Operational Agent's scope, through the same resolver.

    An agent's *authority* is its A0 capability bindings plus the
    autonomy contract -- it does not call the HTTP API, the CC-resident
    evaluator does. What converges here is WHERE, which is what "no
    hidden expansion of scope" is about. `role_permissions=["*"]` says
    "the grant carries no permission narrowing", not "the agent may do
    anything": every action it proposes still passes the bindings, the
    autonomy contract and the node funnel.
    """
    return await load_scope(
        session,
        tenant_id=tenant_id,
        principal_ref=agent_id,
        role_permissions=["*"],
        principal_type=PRINCIPAL_AGENT,
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


async def load_attention(
    session: AsyncSession,
    *,
    tenant_id: str,
    site_id: Optional[str] = None,
) -> dict:
    """Fetch every input and compose the attention answer.

    Same argument as the autonomy loader: `/api/attention` and the
    Operational Agent evaluator must rank the same devices from the same
    evidence, or the agent would act on a picture the operator has never
    seen. The composition stays pure in `harkeniq_cc.attention`.
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

    devices = await FleetCacheRepo(session).list_all(tenant_id)
    if site_id:
        devices = [d for d in devices if d.site_id == site_id]
    outcomes = await OutcomeHistoryRepo(session).list_device_outcome_dicts(tenant_id)
    warranty_map = await WarrantyRepo(session).get_map(
        [d.service_tag for d in devices], tenant_id=tenant_id,
    )
    cve_entries = await CveFeedRepo(session).list_all(tenant_id=tenant_id)
    pending_routes = await ApprovalRouteRepo(session).list_pending(tenant_id)
    patterns = await FleetPatternRepo(session).list_patterns(tenant_id=tenant_id)
    sites = await SiteRepo(session).list_all(tenant_id)
    learned = await LearnedSignalRepo(session).list_active(tenant_id)
    open_incidents = await IncidentRepo(session).list_incidents(
        tenant_id, status="open", site_id=site_id, limit=1000,
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

    return build_attention(
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
