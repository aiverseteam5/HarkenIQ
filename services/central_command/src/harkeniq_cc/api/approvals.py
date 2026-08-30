"""Approvals API: action routing and decision endpoints."""

from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_cc.api.deps import get_cc_state, get_session, require_permission
from harkeniq_cc.auth import UserContext
from harkeniq_cc.db.repos import (
    AgentProposalRepo,
    ApprovalRouteRepo,
    AuditRepo,
    SiteRepo,
)
from harkeniq_cc.sm_client import SMClient

logger = logging.getLogger("harkeniq.cc.api.approvals")

router = APIRouter(prefix="/api/approvals", tags=["approvals"])


def _route_dict(route) -> dict:
    return {
        # A1: one queue, two requesters. `origin` says who asked; the
        # permission, the decision and the downstream execution funnel
        # are identical either way.
        "origin": "node",
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


def _proposal_item(proposal) -> dict:
    """One agent proposal as a queue item.

    Carries the SAME envelope a node-originated action carries so a
    consumer can render one list, plus the agent context a human needs
    to decide: who proposed it, what it observed, the evidence, the
    governance verdict and what is blocking it.
    """
    from harkeniq_cc.api.operational_agents import proposal_dict

    return {
        "origin": "agent",
        "id": proposal.id,
        "site_id": proposal.site_id,
        # `action_id` is the proposal id for an agent item: there is no
        # node action yet, and there will not be one until a human (or
        # the tenant's autonomy grant) decides this.
        "action_id": proposal.id,
        "action_type": proposal.action_type,
        "device_agent_id": proposal.device_agent_id,
        "decision": (
            "approved" if proposal.status in ("approved", "dispatched",
                                              "completed", "failed")
            else "denied" if proposal.status == "denied"
            else None
        ),
        "decided_by": proposal.decided_by or None,
        "decided_at": (
            proposal.decided_at.isoformat() if proposal.decided_at else None
        ),
        "routed_at": (
            proposal.created_at.isoformat() if proposal.created_at else None
        ),
        "delivered_at": (
            proposal.dispatched_at.isoformat() if proposal.dispatched_at else None
        ),
        "proposal": proposal_dict(proposal),
    }


class BatchDecisionRequest(BaseModel):
    action_ids: list[str]
    decision: str  # "approved" or "denied"


async def _decide_agent_proposal(
    proposal_id: str,
    decision: str,
    user: UserContext,
    session: AsyncSession,
    state,
) -> dict:
    """Decide an Operational Agent's proposal (A1).

    Same permission, same endpoint, same audit vocabulary as a node
    action. What differs is only the delivery mechanism underneath: an
    approved proposal is dispatched to the site that owns the device on
    the one-shot CC->SM verb, which queues it on the existing directive
    transport. The node then runs its unchanged gate funnel and can
    still refuse.

    Denial is final (D16): a denied proposal is never re-dispatched, and
    the agent's dedupe key stops it re-proposing the same work.
    """
    repo = AgentProposalRepo(session)
    proposal = await repo.get(user.tenant_id, proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="approval route not found")
    if proposal.status != "awaiting_approval":
        raise HTTPException(
            status_code=409,
            detail=f"proposal already {proposal.status}",
        )
    decided_by = user.email or user.user_id
    await repo.decide(proposal, decision, decided_by)

    delivery = {"accepted": False, "delivered": False, "reason": "not attempted"}
    if decision == "approved":
        site = await SiteRepo(session).get_by_id(proposal.site_id)
        if site is None or site.tenant_id != user.tenant_id:
            await repo.mark_failed(
                proposal, "site is no longer registered to this tenant",
            )
            delivery["reason"] = "site not registered"
        else:
            try:
                client = SMClient(state.config.sm_tls_ca)
                result = await client.dispatch_action(
                    site.sm_endpoint,
                    site.sm_token or "",
                    tenant_id=user.tenant_id,
                    site_id=proposal.site_id,
                    device_agent_id=proposal.device_agent_id,
                    action_type=proposal.action_type,
                    params_json=json.dumps(proposal.params or {}),
                    actor=proposal.actor,
                    # A human decided this one, so the node may treat an
                    # authorization-shaped lease verdict as satisfied.
                    authorization="human_approval",
                    decided_by=decided_by,
                    proposal_id=proposal.id,
                )
                if result.get("accepted"):
                    await repo.mark_dispatched(
                        proposal, result.get("directive_id", ""),
                    )
                    delivery = {
                        "accepted": True,
                        "delivered": True,
                        "directive_id": result.get("directive_id", ""),
                        "reason": "",
                    }
                else:
                    # The site refused. The human's decision stands and
                    # is recorded; the work did not happen and says why.
                    await repo.mark_failed(
                        proposal, result.get("reason", "refused by site"),
                    )
                    delivery = {
                        "accepted": False,
                        "delivered": False,
                        "reason": result.get("reason", "refused by site"),
                    }
            except Exception as exc:  # noqa: BLE001
                # Leave it approved: the background dispatch pass retries,
                # so a site being briefly unreachable never silently
                # discards a human's approval.
                logger.warning(
                    "Dispatch failed for proposal %s: %s", proposal.id, exc,
                )
                delivery = {
                    "accepted": False, "delivered": False, "reason": str(exc),
                }

    await AuditRepo(session).append(
        actor=decided_by,
        action=f"action.{decision}",
        subject=proposal.id,
        tenant_id=user.tenant_id,
        detail={
            "origin": "agent",
            "agent_actor": proposal.actor,
            "site_id": proposal.site_id,
            "action_type": proposal.action_type,
            "device_agent_id": proposal.device_agent_id,
            "delivered": delivery.get("delivered", False),
        },
    )
    await session.commit()
    return {
        "action_id": proposal.id,
        "origin": "agent",
        "decision": decision,
        "decided_by": decided_by,
        "delivery": delivery,
        "proposal": _proposal_item(proposal)["proposal"],
    }


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
        # A1: the same id space serves both origins. An id that is not a
        # node action may be an agent proposal; only if it is neither is
        # this a 404.
        return await _decide_agent_proposal(
            action_id, decision, user, session, state,
        )

    # Verify tenant ownership
    site = await SiteRepo(session).get_by_id(route.site_id)
    if site is None or site.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="approval route not found")

    if route.decision is not None:
        raise HTTPException(
            status_code=409,
            detail=f"action already decided: {route.decision}",
        )

    # QA-006: attribution uses the authenticated identity, preferring
    # the human-readable email from the validated JWT.
    decided_by = user.email or user.user_id
    await repo.update_decision(route, decision, decided_by)

    # Route to the SM via gRPC
    delivery = {"accepted": False, "delivered": False, "reason": "not attempted"}
    try:
        client = SMClient(state.config.sm_tls_ca)
        delivery = await client.route_approval(
            sm_endpoint=site.sm_endpoint,
            token=site.sm_token or "",
            action_id=action_id,
            decision=decision,
            decided_by=decided_by,
            tenant_id=user.tenant_id,
        )
        if delivery.get("delivered"):
            await repo.mark_delivered(route)
    except Exception as exc:
        logger.warning("RouteApproval RPC failed for action %s: %s", action_id, exc)
        delivery = {"accepted": False, "delivered": False, "reason": str(exc)}

    await AuditRepo(session).append(
        actor=decided_by,
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
        "decided_by": decided_by,
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
    """Everything waiting on a human decision, whoever asked.

    A1: agent proposals appear here, in this list, under this
    permission. There is no second approval queue and no agent-only
    review surface: the operator looks in one place, and an agent's
    request earns no different treatment from a node's.

    Agent proposals are prepended rather than paginated alongside the
    routes: they carry a rationale and evidence a human should see
    first, and there are far fewer of them.
    """
    routes, total = await ApprovalRouteRepo(session).list_pending_paginated(
        user.tenant_id, page=page, page_size=page_size,
    )
    proposals = await AgentProposalRepo(session).list_awaiting_approval(
        user.tenant_id
    )
    items = (
        [_proposal_item(p) for p in proposals] if page == 1 else []
    ) + [_route_dict(r) for r in routes]
    return {
        "actions": items,
        "page": page,
        "page_size": page_size,
        "total": total + len(proposals),
        "node_total": total,
        "agent_total": len(proposals),
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
        "decided_by": user.email or user.user_id,
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
