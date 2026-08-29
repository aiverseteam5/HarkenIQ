"""Attention API: what deserves attention first in this tenant, and why.

S2 (2026-08-29). Read-only prioritization over evidence that already
exists. This router only FETCHES tenant-scoped inputs and hands them to
the pure composer in `harkeniq_cc.attention`; all judgement lives there
and is unit-testable without a database.

Governance: `fleet.view` (read-only intelligence). It names governed
capabilities in `recommended_next` but performs none of them and confers
no authority — invoking anything still goes through that capability's own
permission and approval path.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_cc.api.deps import get_session, require_permission
from harkeniq_cc.attention import build_attention
from harkeniq_cc.auth import UserContext
from harkeniq_cc.db.repos import (
    ApprovalRouteRepo,
    CveFeedRepo,
    FleetCacheRepo,
    FleetPatternRepo,
    OutcomeHistoryRepo,
    SiteRepo,
    WarrantyRepo,
)
from harkeniq_cc.exposure import match_exposures
from harkeniq_cc.predictive import (
    cohort_failure_rates,
    score_device,
)
from harkeniq_cc.warranty.base import warranty_status

router = APIRouter(prefix="/api/attention", tags=["attention"])


@router.get(
    "/",
    dependencies=[Depends(require_permission("fleet.view"))],
)
async def attention(
    site_id: str | None = Query(None, description="restrict to one site"),
    band: str | None = Query(
        None, description="filter: high|medium|low|insufficient_data"
    ),
    limit: int = Query(200, ge=1, le=1000),
    user: UserContext = Depends(require_permission("fleet.view")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Ranked attention list plus a site rollup, with evidence and the
    next governed capability for each device.

    Every read below is tenant-scoped by its repository; `site_id` narrows
    within the tenant and can never widen beyond it.
    """
    tenant_id = user.tenant_id

    devices = await FleetCacheRepo(session).list_all(tenant_id)
    if site_id:
        devices = [d for d in devices if d.site_id == site_id]

    outcomes = await OutcomeHistoryRepo(session).list_device_outcome_dicts(tenant_id)
    warranty_map = await WarrantyRepo(session).get_map(
        [d.service_tag for d in devices], tenant_id=tenant_id
    )
    cve_entries = await CveFeedRepo(session).list_all(tenant_id=tenant_id)
    pending_routes = await ApprovalRouteRepo(session).list_pending(tenant_id)
    patterns = await FleetPatternRepo(session).list_patterns(tenant_id=tenant_id)
    sites = await SiteRepo(session).list_all(tenant_id)

    # Score with the existing model — this endpoint adds no risk maths.
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
        if band and risk.band != band:
            continue
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
    )
    # Rank is assigned before truncation, so "rank 1" always means first in
    # the tenant, never first on the page.
    result["items"] = result["items"][:limit]
    result["returned"] = len(result["items"])
    return result
