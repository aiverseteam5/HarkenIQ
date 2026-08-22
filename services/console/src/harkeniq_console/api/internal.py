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
