"""Predictive maintenance API (R4-3 P20).

On-demand per-device failure risk over accumulated outcome history,
enriched with current health and warranty status. Deterministic scoring
(see harkeniq_cc.predictive); no trained model yet by design.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_cc.api.deps import get_scope, get_session, require_permission
from harkeniq_cc.auth import UserContext
from harkeniq_cc.db.repos import FleetCacheRepo, OutcomeHistoryRepo, WarrantyRepo
from harkeniq_cc.predictive import cohort_failure_rates, score_device
from harkeniq_cc.warranty.base import warranty_status

router = APIRouter(prefix="/api/predictive", tags=["predictive"])

_BAND_ORDER = {"high": 0, "medium": 1, "low": 2, "insufficient_data": 3}


@router.get(
    "/risk",
    dependencies=[Depends(require_permission("fleet.view"))],
)
async def device_risk(
    band: str | None = Query(None, description="filter: high|medium|low|insufficient_data"),
    site_id: str | None = Query(None, description="filter to one site's devices"),
    user: UserContext = Depends(require_permission("fleet.view")),
    session: AsyncSession = Depends(get_session),
    scope=Depends(get_scope),
) -> dict:
    """Per-device failure risk, riskiest first.

    A23: one row per DEVICE, so the device list is the caller's scope
    (E1.2 layer 2), not the tenant's. The cohort prior is still computed
    over the tenant's outcomes -- an aggregate rate names no device.
    """
    devices = await FleetCacheRepo(session).list_all(user.tenant_id, scope=scope)
    outcomes = await OutcomeHistoryRepo(session).list_device_outcome_dicts(
        user.tenant_id
    )
    warranty_map = await WarrantyRepo(session).get_map(
        [d.service_tag for d in devices], tenant_id=user.tenant_id
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
        # S2: attach device identity so a row can be placed on a site and
        # a site-scoped caller can filter to its own scope.
        risk.site_id = dev.site_id
        risk.agent_name = dev.agent_name
        if band and risk.band != band:
            continue
        if site_id and dev.site_id != site_id:
            continue
        risks.append(risk)

    risks.sort(key=lambda r: (_BAND_ORDER.get(r.band, 9), -r.risk_score))
    return {
        "risks": [r.to_dict() for r in risks],
        "devices_scored": len(devices),
        "outcomes_considered": len(outcomes),
        "tenant_id": user.tenant_id,
    }
