"""Firmware API: CVE feed import + fleet exposure scan (R4-2 P14).

The feed is local and operator-imported (air-gap safe). Exposure is
computed on demand: every fleet-cache device with a firmware inventory
is matched against the feed using the shared cross-vendor version
comparator (harkeniq.compliance.versions).
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_cc.api.deps import forbid_out_of_scope, get_scope, get_session, require_permission
from harkeniq_cc.auth import UserContext
from harkeniq_cc.db.repos import CveFeedRepo, FleetCacheRepo
from harkeniq_cc.exposure import match_exposures  # noqa: F401  (re-exported)

router = APIRouter(prefix="/api/firmware", tags=["firmware"])


@router.post(
    "/cve-feed",
    # P0 2026-08-29: writes are site.manage, not the read-grade fleet.view
    # this route declared before real role grants existed (C1 follow-on).
    dependencies=[Depends(require_permission("site.manage"))],
)
async def import_cve_feed(
    payload: dict = Body(...),
    user: UserContext = Depends(require_permission("site.manage")),
    session: AsyncSession = Depends(get_session),
    scope=Depends(get_scope),
) -> dict:
    """Import CVE feed entries from an offline bundle: {"entries": [...]}.

    A23: the feed is tenant governance with no site dimension, so the
    write is TENANT authority -- a cluster-scoped site.manage holder may
    read the feed and may not rewrite it for the whole estate.
    """
    forbid_out_of_scope(scope, "site.manage", what="the CVE feed", tenant_object=True)
    entries = payload.get("entries", [])
    imported = await CveFeedRepo(session).import_entries(
        entries if isinstance(entries, list) else [],
        tenant_id=user.tenant_id,
    )
    await session.commit()
    return {"imported": imported, "tenant_id": user.tenant_id}


@router.get(
    "/cve-feed",
    dependencies=[Depends(require_permission("fleet.view"))],
)
async def list_cve_feed(
    user: UserContext = Depends(require_permission("fleet.view")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    rows = await CveFeedRepo(session).list_all(tenant_id=user.tenant_id)
    return {
        "entries": [
            {
                "cve_id": r.cve_id,
                "vendor": r.vendor,
                "component": r.component,
                "affected_versions": r.affected_versions,
                "fixed_version": r.fixed_version,
                "severity": r.severity,
                "description": r.description,
                "published": r.published,
            }
            for r in rows
        ],
        "tenant_id": user.tenant_id,
    }


@router.get(
    "/exposure",
    dependencies=[Depends(require_permission("fleet.view"))],
)
async def firmware_exposure(
    user: UserContext = Depends(require_permission("fleet.view")),
    session: AsyncSession = Depends(get_session),
    scope=Depends(get_scope),
) -> dict:
    """CVE exposure: devices IN THE CALLER'S SCOPE whose firmware matches.

    A23: one row per exposed device, so the device list is scoped.
    """
    devices = await FleetCacheRepo(session).list_all(user.tenant_id, scope=scope)
    entries = await CveFeedRepo(session).list_all(tenant_id=user.tenant_id)
    exposures = match_exposures(devices, entries)
    return {
        "exposures": exposures,
        "devices_scanned": len(devices),
        "feed_entries": len(entries),
        "tenant_id": user.tenant_id,
    }
