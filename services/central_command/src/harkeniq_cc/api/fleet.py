"""Fleet API: cross-site device and incident views."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from harkeniq_cc.api.deps import get_current_user
from harkeniq_cc.auth import UserContext

router = APIRouter(prefix="/api/fleet", tags=["fleet"])


@router.get("/", dependencies=[Depends(get_current_user)])
async def list_devices(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    site_id: str | None = None,
    vendor: str | None = None,
    health: str | None = None,
    user: UserContext = Depends(get_current_user),
) -> dict:
    """List devices across all sites, paginated and filtered."""
    return {
        "devices": [],
        "page": page,
        "page_size": page_size,
        "total": 0,
        "tenant_id": user.tenant_id,
    }


@router.get("/summary", dependencies=[Depends(get_current_user)])
async def fleet_summary(
    user: UserContext = Depends(get_current_user),
) -> dict:
    """KPI aggregates across the tenant's fleet."""
    return {
        "total_devices": 0,
        "total_sites": 0,
        "healthy": 0,
        "warning": 0,
        "critical": 0,
        "unobserved": 0,
        "tenant_id": user.tenant_id,
    }


@router.get("/incidents", dependencies=[Depends(get_current_user)])
async def list_incidents(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    status: str | None = None,
    user: UserContext = Depends(get_current_user),
) -> dict:
    """Cross-site incident list."""
    return {
        "incidents": [],
        "page": page,
        "page_size": page_size,
        "total": 0,
        "tenant_id": user.tenant_id,
    }


@router.get("/incidents/{incident_id}", dependencies=[Depends(get_current_user)])
async def get_incident(
    incident_id: str,
    user: UserContext = Depends(get_current_user),
) -> dict:
    """Incident detail."""
    return {
        "incident_id": incident_id,
        "detail": None,
        "tenant_id": user.tenant_id,
    }
