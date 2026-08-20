"""Sites API: site registration and listing."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from harkeniq_cc.api.deps import get_current_user
from harkeniq_cc.auth import UserContext

router = APIRouter(prefix="/api/sites", tags=["sites"])


@router.post("/register", dependencies=[Depends(get_current_user)])
async def register_site(
    user: UserContext = Depends(get_current_user),
) -> dict:
    """Register a new Site Manager with Central Command."""
    return {
        "registered": False,
        "detail": "stub",
        "tenant_id": user.tenant_id,
    }


@router.get("/", dependencies=[Depends(get_current_user)])
async def list_sites(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    user: UserContext = Depends(get_current_user),
) -> dict:
    """List registered sites."""
    return {
        "sites": [],
        "page": page,
        "page_size": page_size,
        "total": 0,
        "tenant_id": user.tenant_id,
    }


@router.get("/{site_id}", dependencies=[Depends(get_current_user)])
async def get_site(
    site_id: str,
    user: UserContext = Depends(get_current_user),
) -> dict:
    """Site detail."""
    return {
        "site_id": site_id,
        "detail": None,
        "tenant_id": user.tenant_id,
    }
