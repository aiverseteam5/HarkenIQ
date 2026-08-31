"""Internal service-to-service endpoints.

Called by Central Command (usage snapshots, marketplace install pulls).
QA-035: authenticated by the shared CC<->Console API key — CC has sent
``Authorization: Bearer <console_api_key>`` since R5-2; the Console never
checked it until now. Secure mode with no key configured fails CLOSED.
"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_console.api.deps import get_session
from harkeniq_console.billing.metering import MeteringService


async def require_internal_key(request: Request) -> None:
    """QA-035: the CC<->Console credential pair, actually enforced."""
    config = request.app.state.console.config
    if config.insecure:
        return
    expected = getattr(config, "internal_api_key", "")
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="internal API key not configured (fail closed)",
        )
    provided = request.headers.get("authorization", "")
    if not hmac.compare_digest(provided, f"Bearer {expected}"):
        raise HTTPException(status_code=401, detail="invalid internal key")


router = APIRouter(
    prefix="/api/internal",
    tags=["internal"],
    dependencies=[Depends(require_internal_key)],
)

_metering = MeteringService()


class UsageEventPayload(BaseModel):
    site_name: str
    date: str
    node_count: int
    agent_versions: dict | None = None


class UsageEventsRequest(BaseModel):
    tenant_id: str
    events: list[UsageEventPayload]


@router.post("/usage-events")
async def ingest_usage_events(
    body: UsageEventsRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    events = [e.model_dump() for e in body.events]
    count = await _metering.ingest_usage_batch(session, body.tenant_id, events)
    await session.commit()
    return {"recorded": count}


@router.get("/marketplace/installs")
async def list_marketplace_installs(
    tenant_id: str,
    since: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """R5-2: install events for a tenant, with the skill payloads.

    Pulled by the tenant's Central Command (CC->Console direction, same
    as usage reporting -- Console never dials CC). `since` is an ISO
    timestamp cursor; CC also dedupes durably on install_id.
    """
    from datetime import datetime

    from harkeniq_console.db.repos import MarketplaceInstallRepo, MarketplaceRepo

    since_dt = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since)
        except ValueError:
            since_dt = None
    installs = await MarketplaceInstallRepo(session).list_for_tenant(
        tenant_id, since=since_dt
    )
    marketplace = MarketplaceRepo(session)
    items = []
    for install in installs:
        entry = await marketplace.get_by_id(install.skill_entry_id)
        if entry is None or not entry.published:
            continue  # unpublished/withdrawn skills are never delivered
        items.append({
            "install_id": install.id,
            "installed_at": install.installed_at.isoformat()
            if install.installed_at else None,
            "installed_by": install.installed_by,
            "skill_name": entry.skill_name,
            "skill_version": entry.version,
            "tier": entry.tier,
            "yaml_content": entry.yaml_content,
        })
    return {"installs": items, "tenant_id": tenant_id}


@router.get("/marketplace/skills/{skill_id}")
async def internal_skill_by_id(
    skill_id: str,
    tenant_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """A2: serve one skill's YAML to Central Command by id.

    The fourth piece E0.3 named when it refused skill bindings rather
    than leave them accepted and inert. It rides the EXISTING CC<->Console
    credential pair on this router, so no new trust direction is created:
    Central Command already pulls marketplace installs here.

    Tenant-scoped deliberately. A published skill is readable by any
    tenant; an unpublished one only by the tenant that owns it, matching
    the tenant-identity read on `/api/marketplace/skills/{id}`. An
    internal caller must not become a way around that.
    """
    from harkeniq_console.db.repos import MarketplaceRepo

    entry = await MarketplaceRepo(session).get_by_id(skill_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="skill not found")
    if not entry.published and entry.tenant_id != tenant_id:
        # Same answer as "does not exist": confirming it would leak that
        # another tenant has a skill by this id.
        raise HTTPException(status_code=404, detail="skill not found")
    return {
        "skill_id": entry.id,
        "name": entry.name,
        "version": entry.version,
        "tier": entry.tier,
        "validation_state": entry.validation_state,
        "published": entry.published,
        "yaml_content": entry.yaml_content or "",
    }
