"""Warranty API: cached records + manual import (R4-2 P15).

The import endpoint is the coverage path for vendors without a public
warranty API (HPE has none for ProLiant as of 2026 -- verified; only
the web lookup exists), and for air-gapped deployments.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_cc.api.deps import get_session, require_permission
from harkeniq_cc.auth import UserContext
from harkeniq_cc.db.repos import WarrantyRepo
from harkeniq_cc.warranty.base import WarrantyRecord, warranty_status

router = APIRouter(prefix="/api/warranty", tags=["warranty"])


def warranty_dict(row) -> dict:
    return {
        "service_tag": row.service_tag,
        "vendor": row.vendor,
        "service_level": row.service_level,
        "start_date": row.start_date,
        "end_date": row.end_date,
        "status": warranty_status(row.end_date),
        "source": row.source,
        "fetched_at": row.fetched_at.isoformat() if row.fetched_at else None,
    }


@router.get(
    "/",
    dependencies=[Depends(require_permission("fleet.view"))],
)
async def list_warranty(
    user: UserContext = Depends(require_permission("fleet.view")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    rows = await WarrantyRepo(session).list_all()
    return {
        "records": [warranty_dict(r) for r in rows],
        "tenant_id": user.tenant_id,
    }


@router.post(
    "/import",
    dependencies=[Depends(require_permission("fleet.view"))],
)
async def import_warranty(
    payload: dict = Body(...),
    user: UserContext = Depends(require_permission("fleet.view")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Import warranty records: {"records": [{service_tag, vendor,
    service_level, start_date, end_date}, ...]}."""
    raw = payload.get("records", [])
    records = [
        WarrantyRecord(
            service_tag=str(r.get("service_tag", "")),
            vendor=str(r.get("vendor", "")),
            service_level=str(r.get("service_level", "")),
            start_date=str(r.get("start_date", ""))[:10],
            end_date=str(r.get("end_date", ""))[:10],
            source="import",
        )
        for r in raw if isinstance(r, dict)
    ]
    imported = await WarrantyRepo(session).upsert_records(records)
    await session.commit()
    return {"imported": imported, "tenant_id": user.tenant_id}
