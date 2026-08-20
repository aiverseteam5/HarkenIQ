"""Platform admin dashboard endpoints."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/dashboard")
async def dashboard() -> dict:
    return {"stub": "dashboard"}


@router.get("/tenants/health")
async def tenants_health() -> dict:
    return {"stub": "tenants_health"}


@router.get("/system")
async def system_info() -> dict:
    return {"stub": "system_info"}


@router.get("/billing/stats")
async def billing_stats() -> dict:
    return {"stub": "billing_stats"}


@router.get("/billing/delinquency")
async def billing_delinquency() -> dict:
    return {"stub": "billing_delinquency"}


@router.get("/billing/cohorts")
async def billing_cohorts() -> dict:
    return {"stub": "billing_cohorts"}
