"""Fleet API: cross-site device and incident views."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_cc.api.deps import get_session, require_permission
from harkeniq_cc.auth import UserContext
from harkeniq_cc.db.repos import FleetCacheRepo, SiteRepo

router = APIRouter(prefix="/api/fleet", tags=["fleet"])


def _device_dict(dev) -> dict:
    return {
        "id": dev.id,
        "site_id": dev.site_id,
        "agent_id": dev.agent_id,
        "agent_name": dev.agent_name,
        "vendor": dev.vendor,
        "model": dev.model,
        "observation": dev.observation,
        "health": dev.health,
        "subsystems": dev.subsystems,
        "snapshot_at": dev.snapshot_at.isoformat() if dev.snapshot_at else None,
    }


@router.get(
    "/",
    dependencies=[Depends(require_permission("fleet.view"))],
)
async def list_devices(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    site_id: str | None = None,
    vendor: str | None = None,
    health: str | None = None,
    search: str | None = None,
    user: UserContext = Depends(require_permission("fleet.view")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """List devices across all sites, paginated and filtered."""
    devices, total = await FleetCacheRepo(session).list_filtered(
        tenant_id=user.tenant_id,
        site_id=site_id,
        vendor=vendor,
        health=health,
        search=search,
        page=page,
        page_size=page_size,
    )
    return {
        "devices": [_device_dict(d) for d in devices],
        "page": page,
        "page_size": page_size,
        "total": total,
        "tenant_id": user.tenant_id,
    }


@router.get(
    "/summary",
    dependencies=[Depends(require_permission("fleet.view"))],
)
async def fleet_summary(
    user: UserContext = Depends(require_permission("fleet.view")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """KPI aggregates across the tenant's fleet."""
    cache = FleetCacheRepo(session)
    by_health = await cache.count_by_health(user.tenant_id)
    total = sum(by_health.values())
    sites_count = await SiteRepo(session).count(user.tenant_id)

    # Incidents open: count devices with critical health as a proxy
    incidents_open = by_health.get("critical", 0)

    return {
        "total_nodes": total,
        "by_health": {
            "ok": by_health.get("ok", 0),
            "warning": by_health.get("warning", 0),
            "critical": by_health.get("critical", 0),
            "unknown": by_health.get("unknown", 0),
        },
        "sites_count": sites_count,
        "incidents_open": incidents_open,
        "tenant_id": user.tenant_id,
    }


@router.get(
    "/incidents",
    dependencies=[Depends(require_permission("fleet.view"))],
)
async def list_incidents(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    status: str | None = None,
    user: UserContext = Depends(require_permission("fleet.view")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Cross-site incident list.

    Placeholder: returns devices with critical health as pseudo-incidents.
    """
    devices, total = await FleetCacheRepo(session).list_filtered(
        tenant_id=user.tenant_id,
        health="critical",
        page=page,
        page_size=page_size,
    )
    incidents = [
        {
            "incident_id": d.id,
            "kind": "critical_health",
            "status": "open",
            "title": f"Critical health on {d.agent_name or d.agent_id}",
            "device_agent_id": d.agent_id,
            "site_id": d.site_id,
            "subsystem": "",
            "opened_at": d.snapshot_at.isoformat() if d.snapshot_at else None,
        }
        for d in devices
    ]
    return {
        "incidents": incidents,
        "page": page,
        "page_size": page_size,
        "total": total,
        "tenant_id": user.tenant_id,
    }


@router.get(
    "/incidents/{incident_id}",
    dependencies=[Depends(require_permission("fleet.view"))],
)
async def get_incident(
    incident_id: str,
    user: UserContext = Depends(require_permission("fleet.view")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Incident detail (placeholder: returns device info if critical)."""
    # incident_id is the fleet_cache row id in this placeholder
    from sqlalchemy import select

    from harkeniq_cc.db.models import CCFleetCache, CCSite

    row = await session.get(CCFleetCache, incident_id)
    if row is None:
        raise HTTPException(status_code=404, detail="incident not found")
    # Verify tenant ownership
    site = await session.get(CCSite, row.site_id)
    if site is None or site.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="incident not found")
    return {
        "incident_id": incident_id,
        "kind": "critical_health",
        "status": "open" if row.health == "critical" else "resolved",
        "title": f"Critical health on {row.agent_name or row.agent_id}",
        "device": _device_dict(row),
        "tenant_id": user.tenant_id,
    }
