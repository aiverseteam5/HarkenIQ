"""Billing and usage endpoints (tenant-scoped)."""

from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File
from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_console.api.deps import get_console_state, get_session, tenant_scope
from harkeniq_console.auth import UserContext
from harkeniq_console.billing.engine import BillingEngine
from harkeniq_console.billing.metering import MeteringService
from harkeniq_console.db.repos import (
    DelinquencyLogRepo,
    SubscriptionRepo,
    TenantRepo,
)

router = APIRouter(prefix="/api/tenants/{tenant_id}", tags=["billing"])

_engine = BillingEngine()
_metering = MeteringService()


def _sub_dict(sub) -> dict:
    return {
        "id": sub.id,
        "tenant_id": sub.tenant_id,
        "plan": sub.plan,
        "node_commit": sub.node_commit,
        "billing_frequency": sub.billing_frequency,
        "billing_cycle_start": str(sub.billing_cycle_start),
        "status": sub.status,
        "price_book_version": sub.price_book_version,
    }


@router.get("/subscription")
async def get_subscription(
    tenant_id: str,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(tenant_scope),
) -> dict:
    tenant = await TenantRepo(session).get_by_id(tenant_id)
    if tenant is None:
        raise HTTPException(404, "tenant not found")
    sub = await SubscriptionRepo(session).get_by_tenant(tenant_id)
    if sub is None:
        raise HTTPException(404, "no subscription found")
    return {
        "subscription": {**_sub_dict(sub), "currency": tenant.currency},
    }


@router.get("/usage")
async def get_usage(
    tenant_id: str,
    period_start: date | None = None,
    period_end: date | None = None,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(tenant_scope),
) -> dict:
    now = datetime.utcnow()
    if period_start is None:
        period_start = now.replace(day=1).date()
    if period_end is None:
        period_end = now.date()
    return await _metering.get_usage_summary(session, tenant_id, period_start, period_end)


@router.get("/usage/estimate")
async def estimate_usage(
    tenant_id: str,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(tenant_scope),
) -> dict:
    try:
        return await _metering.estimate_upcoming_trueup(session, tenant_id)
    except ValueError:
        # QA ISSUE-006: unknown tenant crashed with a 500
        raise HTTPException(404, "tenant not found")


@router.post("/usage/upload")
async def upload_usage(
    tenant_id: str,
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
    state=Depends(get_console_state),
    user: UserContext = Depends(tenant_scope),
) -> dict:
    report_bytes = await file.read()
    public_key_pem = ""
    if state.config.license_verify_key_path:
        import pathlib
        key_path = pathlib.Path(state.config.license_verify_key_path)
        if key_path.exists():
            public_key_pem = key_path.read_text()
    if not public_key_pem:
        raise HTTPException(400, "no usage verification key configured")
    try:
        result = await _metering.upload_signed_usage_report(
            session, tenant_id, report_bytes, public_key_pem,
        )
        await session.commit()
        return result
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/usage/sites")
async def usage_sites(
    tenant_id: str,
    period_start: date | None = None,
    period_end: date | None = None,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(tenant_scope),
) -> dict:
    now = datetime.utcnow()
    if period_start is None:
        period_start = now.replace(day=1).date()
    if period_end is None:
        period_end = now.date()
    from harkeniq_console.db.repos import UsageEventRepo
    sites = await UsageEventRepo(session).get_per_site_summary(
        tenant_id, period_start, period_end,
    )
    return {"items": sites}


@router.get("/delinquency")
async def get_delinquency(
    tenant_id: str,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(tenant_scope),
) -> dict:
    tenant = await TenantRepo(session).get_by_id(tenant_id)
    if tenant is None:
        raise HTTPException(404, "tenant not found")
    return {
        "status": tenant.delinquency_status,
        "days_overdue": 0,
        "overdue_amount_cents": 0,
    }
