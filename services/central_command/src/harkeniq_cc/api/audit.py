"""Audit API: paginated audit log entries."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from harkeniq_cc.api.deps import get_current_user
from harkeniq_cc.auth import UserContext

router = APIRouter(prefix="/api/audit", tags=["audit"])


@router.get("/", dependencies=[Depends(get_current_user)])
async def list_audit(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    actor: str | None = None,
    action: str | None = None,
    user: UserContext = Depends(get_current_user),
) -> dict:
    """Paginated audit entries."""
    return {
        "entries": [],
        "page": page,
        "page_size": page_size,
        "total": 0,
        "tenant_id": user.tenant_id,
    }
