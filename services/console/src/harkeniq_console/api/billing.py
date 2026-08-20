"""Billing and usage endpoints."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/tenants/{tenant_id}", tags=["billing"])


@router.get("/subscription")
async def get_subscription(tenant_id: str) -> dict:
    return {"stub": "get_subscription", "tenant_id": tenant_id}


@router.get("/usage")
async def get_usage(tenant_id: str) -> dict:
    return {"stub": "get_usage", "tenant_id": tenant_id}


@router.get("/usage/estimate")
async def estimate_usage(tenant_id: str) -> dict:
    return {"stub": "estimate_usage", "tenant_id": tenant_id}


@router.post("/usage/upload")
async def upload_usage(tenant_id: str) -> dict:
    return {"stub": "upload_usage", "tenant_id": tenant_id}
