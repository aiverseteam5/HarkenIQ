"""Approvals API: action routing and decision endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from harkeniq_cc.api.deps import get_current_user
from harkeniq_cc.auth import UserContext

router = APIRouter(prefix="/api/approvals", tags=["approvals"])


@router.get("/", dependencies=[Depends(get_current_user)])
async def list_pending(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    user: UserContext = Depends(get_current_user),
) -> dict:
    """List pending approval actions."""
    return {
        "actions": [],
        "page": page,
        "page_size": page_size,
        "total": 0,
        "tenant_id": user.tenant_id,
    }


@router.post("/{action_id}/approve", dependencies=[Depends(get_current_user)])
async def approve_action(
    action_id: str,
    user: UserContext = Depends(get_current_user),
) -> dict:
    """Approve a pending action."""
    return {
        "action_id": action_id,
        "decision": "approved",
        "decided_by": user.user_id,
    }


@router.post("/{action_id}/deny", dependencies=[Depends(get_current_user)])
async def deny_action(
    action_id: str,
    user: UserContext = Depends(get_current_user),
) -> dict:
    """Deny a pending action."""
    return {
        "action_id": action_id,
        "decision": "denied",
        "decided_by": user.user_id,
    }


@router.post("/batch", dependencies=[Depends(get_current_user)])
async def batch_decide(
    user: UserContext = Depends(get_current_user),
) -> dict:
    """Batch approve/deny multiple actions."""
    return {
        "processed": 0,
        "decided_by": user.user_id,
    }


@router.get("/history", dependencies=[Depends(get_current_user)])
async def approval_history(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    user: UserContext = Depends(get_current_user),
) -> dict:
    """History of approval decisions."""
    return {
        "actions": [],
        "page": page,
        "page_size": page_size,
        "total": 0,
        "tenant_id": user.tenant_id,
    }
