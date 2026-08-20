"""License management endpoints."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/tenants/{tenant_id}/licenses", tags=["licenses"])


@router.post("/")
async def create_license(tenant_id: str) -> dict:
    return {"stub": "create_license", "tenant_id": tenant_id}


@router.get("/")
async def list_licenses(tenant_id: str) -> dict:
    return {"stub": "list_licenses", "tenant_id": tenant_id}


@router.get("/{license_id}")
async def get_license(tenant_id: str, license_id: str) -> dict:
    return {"stub": "get_license", "tenant_id": tenant_id, "license_id": license_id}


@router.get("/{license_id}/download")
async def download_license(tenant_id: str, license_id: str) -> dict:
    return {"stub": "download_license", "tenant_id": tenant_id, "license_id": license_id}


@router.post("/{license_id}/revoke")
async def revoke_license(tenant_id: str, license_id: str) -> dict:
    return {"stub": "revoke_license", "tenant_id": tenant_id, "license_id": license_id}


# ── validation (no tenant scope) ───────────────────────────────────

validate_router = APIRouter(prefix="/api/licenses", tags=["licenses"])


@validate_router.post("/validate")
async def validate_license() -> dict:
    return {"stub": "validate_license"}
