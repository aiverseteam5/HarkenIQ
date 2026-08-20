"""Internal service-to-service endpoints."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/internal", tags=["internal"])


@router.post("/usage-events")
async def ingest_usage_events() -> dict:
    return {"stub": "ingest_usage_events"}
