"""Sites API: site registration and listing."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_cc.api.deps import forbid_out_of_scope, get_cc_state, get_scope, get_session, require_permission
from harkeniq_cc.auth import UserContext
from harkeniq_cc.db.repos import AuditRepo, FleetCacheRepo, OrgUnitRepo, SiteRepo
from harkeniq_cc.sm_client import SMClient

logger = logging.getLogger("harkeniq.cc.api.sites")

router = APIRouter(prefix="/api/sites", tags=["sites"])


class SiteOrgUnitRequest(BaseModel):
    """E1.1: the one organizational node this site hangs from."""

    org_unit_id: str


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
        "org_unit_id": site.org_unit_id,
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
    scope=Depends(get_scope),
    state=Depends(get_cc_state),
) -> dict:
    """Register a new Site Manager with Central Command.

    Stores the site locally, then calls RegisterSite on the SM via gRPC.

    E1.2: registering a site is a TENANT-level act -- the site does not
    exist yet, so there is no object to scope it to. A cluster manager
    cannot conjure a new site into the tenant.
    """
    forbid_out_of_scope(
        scope, "site.manage", what="registering a new site", tenant_object=True
    )
    # QA-019: when CC holds a verified license, its fingerprint IS the
    # registration credential — never a caller-typed string. A mismatched
    # body value is rejected; without a loaded license (lab), the body
    # passthrough remains.
    fingerprint = body.license_fingerprint
    lic = getattr(state, "license", None)
    if lic is not None:
        if fingerprint and fingerprint != lic.fingerprint:
            raise HTTPException(
                status_code=400,
                detail="license_fingerprint does not match this CC's "
                       "verified license",
            )
        fingerprint = lic.fingerprint

    repo = SiteRepo(session)
    existing = await repo.get_by_name(user.tenant_id, body.site_name)
    if existing is not None and existing.sm_token:
        raise HTTPException(
            status_code=409,
            detail=f"site '{body.site_name}' already registered",
        )
    # QA-037: an existing row WITHOUT a token is a half-registration (the
    # RegisterSite RPC failed after the row was created) — re-running the
    # registration must heal it, not 409 forever.
    site = existing or await repo.upsert(
        tenant_id=user.tenant_id,
        site_name=body.site_name,
        sm_endpoint=body.sm_endpoint,
        license_fingerprint=fingerprint,
    )

    # Attempt SM registration via gRPC
    sm_result = {"accepted": False, "site_token": "", "reason": "not attempted"}
    try:
        client = SMClient(state.config.sm_tls_ca)
        sm_result = await client.register_site(
            sm_endpoint=body.sm_endpoint,
            tenant_id=user.tenant_id,
            site_name=body.site_name,
            license_fingerprint=fingerprint,
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
    scope=Depends(get_scope),
) -> dict:
    """List registered sites for the tenant."""
    sites = await SiteRepo(session).list_all(user.tenant_id, scope=scope)
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
    scope=Depends(get_scope),
) -> dict:
    """Site detail with device count."""
    site = await SiteRepo(session).get_by_id(site_id)
    if site is None or site.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="site not found")
    # E1.2: out of scope reads as absent.
    if not scope.covers_site(site.id):
        raise HTTPException(status_code=404, detail="site not found")

    devices = await FleetCacheRepo(session).list_by_site(site_id)
    result = _site_dict(site)
    result["device_count"] = len(devices)
    return result


@router.put("/{site_id}/org-unit")
async def set_site_org_unit(
    site_id: str,
    body: SiteOrgUnitRequest,
    user: UserContext = Depends(require_permission("site.manage")),
    session: AsyncSession = Depends(get_session),
    scope=Depends(get_scope),
) -> dict:
    """Attach this site to an organizational unit, or move it (E1.1).

    One canonical containment path per site: setting a new unit clears
    the old one, because a site sitting in two places at once would make
    "the sites under Region West" ambiguous -- and at E1.2 it would make
    a scope grant ambiguous too.

    Containment only. This changes who owns the site on the org chart
    and changes nothing about who may act on it.
    """
    site = await SiteRepo(session).get_by_id(site_id)
    if site is None or site.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="site not found")

    org_repo = OrgUnitRepo(session)
    unit = await org_repo.get(user.tenant_id, body.org_unit_id)
    if unit is None:
        raise HTTPException(status_code=404, detail="org unit not found")

    # E1.2 layer 3, on BOTH ends. Moving a site needs authority over
    # where it is and over where it is going -- otherwise a cluster
    # manager could pull a site into their own reach, or push one out
    # of it, and either direction is an unreviewed authority change.
    forbid_out_of_scope(
        scope, "site.manage", what=f"site {site.site_name!r}", site_id=site.id
    )
    forbid_out_of_scope(
        scope, "site.manage",
        what=f"org unit {unit.name!r}", org_unit_path=unit.path,
    )

    previous = site.org_unit_id
    if previous == unit.id:
        return {"site_id": site.id, "org_unit_id": unit.id, "changed": False}

    site.org_unit_id = unit.id
    await AuditRepo(session).append(
        actor=user.user_id,
        action="org_unit.site_attached",
        subject=site.id,
        tenant_id=user.tenant_id,
        detail={
            "site_name": site.site_name,
            "from_org_unit_id": previous,
            "to_org_unit_id": unit.id,
            "to_path": unit.path,
        },
    )
    await session.commit()
    return {
        "site_id": site.id,
        "org_unit_id": unit.id,
        "org_unit_path": unit.path,
        "changed": True,
    }
