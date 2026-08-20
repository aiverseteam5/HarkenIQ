"""Sites API: site registration and listing."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_cc.api.deps import get_cc_state, get_session, require_permission
from harkeniq_cc.auth import UserContext
from harkeniq_cc.db.repos import AuditRepo, FleetCacheRepo, SiteRepo
from harkeniq_cc.sm_client import SMClient

logger = logging.getLogger("harkeniq.cc.api.sites")

router = APIRouter(prefix="/api/sites", tags=["sites"])


class SiteRegisterRequest(BaseModel):
    site_name: str
    sm_endpoint: str
    license_fingerprint: str = ""


def _site_dict(site) -> dict:
    return {
        "id": site.id,
        "tenant_id": site.tenant_id,
        "site_name": site.site_name,
        "sm_endpoint": site.sm_endpoint,
        "status": site.status,
        "license_fingerprint": site.license_fingerprint,
        "registered_at": site.registered_at.isoformat() if site.registered_at else None,
        "last_seen_at": site.last_seen_at.isoformat() if site.last_seen_at else None,
    }


@router.post(
    "/register",
    dependencies=[Depends(require_permission("site.manage"))],
)
async def register_site(
    body: SiteRegisterRequest,
    user: UserContext = Depends(require_permission("site.manage")),
    session: AsyncSession = Depends(get_session),
    state=Depends(get_cc_state),
) -> dict:
    """Register a new Site Manager with Central Command.

    Stores the site locally, then calls RegisterSite on the SM via gRPC.
    """
    repo = SiteRepo(session)
    existing = await repo.get_by_name(user.tenant_id, body.site_name)
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"site '{body.site_name}' already registered",
        )

    site = await repo.upsert(
        tenant_id=user.tenant_id,
        site_name=body.site_name,
        sm_endpoint=body.sm_endpoint,
        license_fingerprint=body.license_fingerprint,
    )

    # Attempt SM registration via gRPC
    sm_result = {"accepted": False, "site_token": "", "reason": "not attempted"}
    try:
        client = SMClient()
        sm_result = await client.register_site(
            sm_endpoint=body.sm_endpoint,
            tenant_id=user.tenant_id,
            site_name=body.site_name,
            license_fingerprint=body.license_fingerprint,
            site_id=site.id,
        )
        if sm_result.get("site_token"):
            site.sm_token = sm_result["site_token"]
            await session.flush()
    except Exception as exc:
        logger.warning("SM RegisterSite RPC failed for %s: %s", body.sm_endpoint, exc)
        sm_result = {"accepted": False, "site_token": "", "reason": str(exc)}

    await AuditRepo(session).append(
        actor=user.user_id,
        action="site.register",
        subject=site.id,
        tenant_id=user.tenant_id,
        detail={
            "site_name": body.site_name,
            "sm_endpoint": body.sm_endpoint,
            "sm_accepted": sm_result.get("accepted", False),
        },
    )
    await session.commit()

    return {
        "registered": True,
        "site": _site_dict(site),
        "sm_registration": sm_result,
    }


@router.get(
    "/",
    dependencies=[Depends(require_permission("fleet.view"))],
)
async def list_sites(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    user: UserContext = Depends(require_permission("fleet.view")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """List registered sites for the tenant."""
    sites = await SiteRepo(session).list_all(user.tenant_id)
    total = len(sites)
    start = (page - 1) * page_size
    end = start + page_size
    return {
        "sites": [_site_dict(s) for s in sites[start:end]],
        "page": page,
        "page_size": page_size,
        "total": total,
        "tenant_id": user.tenant_id,
    }


@router.get(
    "/{site_id}",
    dependencies=[Depends(require_permission("fleet.view"))],
)
async def get_site(
    site_id: str,
    user: UserContext = Depends(require_permission("fleet.view")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Site detail with device count."""
    site = await SiteRepo(session).get_by_id(site_id)
    if site is None or site.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="site not found")

    devices = await FleetCacheRepo(session).list_by_site(site_id)
    result = _site_dict(site)
    result["device_count"] = len(devices)
    return result
