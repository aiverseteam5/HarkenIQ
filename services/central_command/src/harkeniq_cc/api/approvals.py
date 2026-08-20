"""Approvals API: action routing and decision endpoints."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_cc.api.deps import get_cc_state, get_session, require_permission
from harkeniq_cc.auth import UserContext
from harkeniq_cc.db.repos import ApprovalRouteRepo, AuditRepo, SiteRepo
from harkeniq_cc.sm_client import SMClient

logger = logging.getLogger("harkeniq.cc.api.approvals")

router = APIRouter(prefix="/api/approvals", tags=["approvals"])


def _route_dict(route) -> dict:
    return {
        "id": route.id,
        "site_id": route.site_id,
        "action_id": route.action_id,
        "action_type": route.action_type,
        "device_agent_id": route.device_agent_id,
        "decision": route.decision,
        "decided_by": route.decided_by,
        "decided_at": route.decided_at.isoformat() if route.decided_at else None,
        "routed_at": route.routed_at.isoformat() if route.routed_at else None,
        "delivered_at": route.delivered_at.isoformat() if route.delivered_at else None,
    }


class BatchDecisionRequest(BaseModel):
    action_ids: list[str]
    decision: str  # "approved" or "denied"


async def _route_decision(
    action_id: str,
    decision: str,
    user: UserContext,
    session: AsyncSession,
    state,
) -> dict:
    """Shared logic for approve/deny: update DB, route to SM, audit-log."""
    repo = ApprovalRouteRepo(session)
    route = await repo.get_by_action_id(action_id)
    if route is None:
        raise HTTPException(status_code=404, detail="approval route not found")

    # Verify tenant ownership
    site = await SiteRepo(session).get_by_id(route.site_id)
    if site is None or site.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="approval route not found")

    if route.decision is not None:
        raise HTTPException(
            status_code=409,
            detail=f"action already decided: {route.decision}",
        )

    # Update the local record
    await repo.update_decision(route, decision, user.user_id)

    # Route to the SM via gRPC
    delivery = {"accepted": False, "delivered": False, "reason": "not attempted"}
    try:
        client = SMClient()
        delivery = await client.route_approval(
            sm_endpoint=site.sm_endpoint,
            token=site.sm_token or "",
            action_id=action_id,
            decision=decision,
            decided_by=user.user_id,
            tenant_id=user.tenant_id,
        )
        if delivery.get("delivered"):
            await repo.mark_delivered(route)
    except Exception as exc:
        logger.warning("RouteApproval RPC failed for action %s: %s", action_id, exc)
        delivery = {"accepted": False, "delivered": False, "reason": str(exc)}

    await AuditRepo(session).append(
        actor=user.user_id,
        action=f"action.{decision}",
        subject=action_id,
        tenant_id=user.tenant_id,
        detail={
            "site_id": route.site_id,
            "action_type": route.action_type,
            "device_agent_id": route.device_agent_id,
            "delivered": delivery.get("delivered", False),
        },
    )
    await session.commit()

    return {
        "action_id": action_id,
        "decision": decision,
        "decided_by": user.user_id,
        "delivery": delivery,
        "route": _route_dict(route),
    }


@router.get(
    "/",
    dependencies=[Depends(require_permission("action.approve"))],
)
async def list_pending(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    user: UserContext = Depends(require_permission("action.approve")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """List pending approval actions."""
    routes, total = await ApprovalRouteRepo(session).list_pending_paginated(
        user.tenant_id, page=page, page_size=page_size,
    )
    return {
        "actions": [_route_dict(r) for r in routes],
        "page": page,
        "page_size": page_size,
        "total": total,
        "tenant_id": user.tenant_id,
    }


@router.post(
    "/{action_id}/approve",
    dependencies=[Depends(require_permission("action.approve"))],
)
async def approve_action(
    action_id: str,
    user: UserContext = Depends(require_permission("action.approve")),
    session: AsyncSession = Depends(get_session),
    state=Depends(get_cc_state),
) -> dict:
    """Approve a pending action and route the decision to the SM."""
    return await _route_decision(action_id, "approved", user, session, state)


@router.post(
    "/{action_id}/deny",
    dependencies=[Depends(require_permission("action.approve"))],
)
async def deny_action(
    action_id: str,
    user: UserContext = Depends(require_permission("action.approve")),
    session: AsyncSession = Depends(get_session),
    state=Depends(get_cc_state),
) -> dict:
    """Deny a pending action and route the decision to the SM."""
    return await _route_decision(action_id, "denied", user, session, state)


@router.post(
    "/batch",
    dependencies=[Depends(require_permission("action.approve"))],
)
async def batch_decide(
    body: BatchDecisionRequest,
    user: UserContext = Depends(require_permission("action.approve")),
    session: AsyncSession = Depends(get_session),
    state=Depends(get_cc_state),
) -> dict:
    """Batch approve/deny multiple actions."""
    if body.decision not in ("approved", "denied"):
        raise HTTPException(
            status_code=400, detail="decision must be 'approved' or 'denied'"
        )

    results = []
    for action_id in body.action_ids:
        try:
            result = await _route_decision(
                action_id, body.decision, user, session, state
            )
            results.append({"action_id": action_id, "ok": True, "detail": result})
        except HTTPException as exc:
            results.append(
                {"action_id": action_id, "ok": False, "detail": exc.detail}
            )

    return {
        "processed": len(results),
        "results": results,
        "decided_by": user.user_id,
    }


@router.get(
    "/history",
    dependencies=[Depends(require_permission("action.approve"))],
)
async def approval_history(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    user: UserContext = Depends(require_permission("action.approve")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """History of approval decisions."""
    routes, total = await ApprovalRouteRepo(session).list_history_paginated(
        user.tenant_id, page=page, page_size=page_size,
    )
    return {
        "actions": [_route_dict(r) for r in routes],
        "page": page,
        "page_size": page_size,
        "total": total,
        "tenant_id": user.tenant_id,
    }
