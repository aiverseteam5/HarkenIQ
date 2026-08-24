"""Internal service-to-service endpoints.

Called by Central Command to report usage snapshots. No auth (internal network).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_console.api.deps import get_session
from harkeniq_console.billing.metering import MeteringService

router = APIRouter(prefix="/api/internal", tags=["internal"])

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
