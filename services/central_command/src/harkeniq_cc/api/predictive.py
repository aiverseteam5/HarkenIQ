"""Predictive maintenance API (R4-3 P20).

On-demand per-device failure risk over accumulated outcome history,
enriched with current health and warranty status. Deterministic scoring
(see harkeniq_cc.predictive); no trained model yet by design.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_cc.api.deps import get_scope, get_session, require_permission
from harkeniq_cc.scope import read_reach
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
    (E1.2 layer 2), not the tenant's.

    A30.40 (D-P2, D-P3, D-P4): so are the OUTCOMES. They used to be the
    tenant's -- "an aggregate rate names no device" -- and that rate was
    published to four decimal places, so two reads either side of one hidden
    outcome solved the hidden cohort exactly; `outcomes_considered` handed
    the tenant total even to a principal with no reach at all; and a moved
    device kept the history it left at a site the reader does not hold. The
    rows are now the reader's: B0b's owner rule, in SQL, before the window.
    Every device's history, the cohort prior (the same function, D-P9) and
    the count come from them. A tenant-wide reach reads every row, as before.

    A30.41: EVERY row -- the database's exact statistics over all of them,
    where the read used to keep the oldest 50,000 and drop the newest.
    """
    from datetime import datetime, timezone

    reach = read_reach(scope, "fleet.view")
    devices = await FleetCacheRepo(session).list_all(user.tenant_id, scope=reach)
    stats = await OutcomeHistoryRepo(session).device_stats(
        user.tenant_id, scope=reach, now=datetime.now(timezone.utc),
    )
    warranty_map = await WarrantyRepo(session).get_map(
        [d.service_tag for d in devices], tenant_id=user.tenant_id
    )
    cohorts = cohort_failure_rates(stats.cohort_tallies)

    risks = []
    for dev in devices:
        warranty = warranty_map.get(dev.service_tag)
        risk = score_device(
            agent_id=dev.agent_id,
            history=stats.devices.get(dev.agent_id),
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
        "outcomes_considered": stats.total,
        "tenant_id": user.tenant_id,
    }
