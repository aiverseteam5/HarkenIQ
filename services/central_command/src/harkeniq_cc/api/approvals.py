"""Approvals API: one queue, one contract, one decision.

Every action waiting on a human arrives here whoever asked for it -- a
node proposed it, or an Operational Agent did -- and is decided on the
same route, under the same `action.approve` permission, against the same
policy, into the same ledger.

E0.1 (2026-08-30) made the policy bind. `cc_approval_policies` has
carried `approval_mode`, `required_approvers` and a group link since R2b;
nothing consulted them at decision time, so a tenant could configure dual
authorization and get single authorization silently. A decision is now a
SET of `cc_approval_records` and the route's `decision` column is a
projection of that set.

Four rules, applied identically to both origins:

  * the governing policy is the most specific active match on
    (action_type, device_type, risk), ties broken deterministically;
  * an approver may decide a subject once -- the database enforces it;
  * when a group is bound, the approver must belong to it;
  * a denial is terminal (D16) and outranks any number of approvals.

Approval still never overrides a safety gate (A10.3): a fully approved
action runs the unchanged node funnel and can still be refused there.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_cc.api.deps import (
    get_cc_state,
    get_session,
    require_any_permission,
    require_permission,
)
from harkeniq_cc.approval_policy import (
    DECISION_APPROVED,
    DECISION_DENIED,
    STATE_APPROVED,
    STATE_DENIED,
    SUBJECT_ACTION,
    SUBJECT_AGENT_PROPOSAL,
    approval_block,
    is_member,
    required_approvers,
    resolve_policy,
)
from harkeniq_cc.auth import UserContext
from harkeniq_cc.autonomy import action_risk_map
from harkeniq_cc.db.repos import (
    AgentProposalRepo,
    ApprovalGroupRepo,
    FleetCacheRepo,
    ApprovalPolicyRepo,
    ApprovalRecordRepo,
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


class ApprovalIncomplete(Exception):
    """Raised when a decision is recorded but the subject is not yet decided.

    Not an error: it is the normal outcome of the first approval under a
    two-approver policy. Carries the progress payload so the caller can
    answer honestly instead of pretending nothing happened.
    """

    def __init__(self, block: dict) -> None:
        super().__init__("awaiting further approvals")
        self.block = block


async def _governing_policy(
    session: AsyncSession, tenant_id: str, action_type: str, device_agent_id: str,
):
    """(policy, group, members) for this action class, or (None, None, []).

    `device_type` comes from the fleet cache's device class when the
    device is known; an unknown device simply does not match a
    device-scoped policy, which is the conservative direction.
    """
    policies = await ApprovalPolicyRepo(session).list_all(tenant_id)
    device_type = ""
    if device_agent_id:
        device = await FleetCacheRepo(session).get_by_agent_id(device_agent_id)
        if device is not None:
            device_type = device.device_class or ""
    risk = action_risk_map().get((action_type or "").upper(), "")
    policy = resolve_policy(
        policies, action_type=action_type, device_type=device_type, risk=risk,
    )
    group = None
    members: list = []
    group_id = getattr(policy, "group_id", None) if policy is not None else None
    if group_id:
        repo = ApprovalGroupRepo(session)
        group = await repo.get_by_id(group_id)
        if group is not None and group.tenant_id != tenant_id:
            # A policy pointing at another tenant's group is a
            # misconfiguration, not an authorization path.
            group = None
        if group is not None:
            members = list(await repo.list_members(group.id))
    return policy, group, members


async def _record_and_evaluate(
    session: AsyncSession,
    *,
    user: UserContext,
    tenant_id: str,
    subject_type: str,
    subject_ref: str,
    action_type: str,
    device_agent_id: str,
    site_id: str,
    decision: str,
) -> dict:
    """Apply the policy to one approver's decision. Returns the progress block.

    Raises HTTPException for a refusal the approver must see (duplicate,
    out of scope, not in the required group) and ApprovalIncomplete when
    the decision was validly recorded but the subject still needs more.
    """
    policy, group, members = await _governing_policy(
        session, tenant_id, action_type, device_agent_id,
    )
    records_repo = ApprovalRecordRepo(session)

    # An approver decides a subject once. The unique constraint is the
    # real guarantee; this check exists to answer with 409 rather than a
    # database error.
    existing = await records_repo.get_by_approver(
        subject_type, subject_ref, user.user_id,
    )
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"you already {existing.decision} this action; a second "
                f"decision from the same approver does not count twice"
            ),
        )

    if group is not None and not is_member(members, user.user_id, user.email or ""):
        raise HTTPException(
            status_code=403,
            detail=(
                f"this action class requires an approver from the "
                f"{group.name!r} group"
            ),
        )

    # E1.2 fills this in. Until scope grants exist every approver's
    # authority is tenant-wide, which is exactly today's behaviour --
    # stated here rather than left implicit so the seam is visible.
    scope_ok = True

    await records_repo.record(
        tenant_id=tenant_id,
        subject_type=subject_type,
        subject_ref=subject_ref,
        approver_ref=user.user_id,
        approver_email=user.email or user.user_id,
        decision=decision,
        policy_id=getattr(policy, "id", "") or "",
        scope_ok=scope_ok,
    )
    # Every approval is audited individually. Auditing only the outcome
    # would make a two-approver decision indistinguishable from a
    # one-approver decision in the record that is supposed to prove it.
    await AuditRepo(session).append(
        actor=user.email or user.user_id,
        action=f"approval.{decision}",
        subject=subject_ref,
        tenant_id=tenant_id,
        detail={
            "subject_type": subject_type,
            "action_type": action_type,
            "device_agent_id": device_agent_id,
            "site_id": site_id,
            "policy_id": getattr(policy, "id", None),
            "group_id": getattr(group, "id", None),
            "scope_ok": scope_ok,
        },
    )

    records = await records_repo.list_for_subject(subject_type, subject_ref)
    block = approval_block(policy, group, records)
    if block["state"] not in (STATE_APPROVED, STATE_DENIED):
        # Valid, recorded, and not yet enough. Commit so the approval is
        # durable even though nothing executes.
        await session.commit()
        raise ApprovalIncomplete(block)
    return block


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

    # E0.1: the SAME policy, ledger and completion rule a node action
    # gets. An agent's request earns no easier path to a decision.
    block = await _record_and_evaluate(
        session,
        user=user,
        tenant_id=user.tenant_id,
        subject_type=SUBJECT_AGENT_PROPOSAL,
        subject_ref=proposal.id,
        action_type=proposal.action_type,
        device_agent_id=proposal.device_agent_id,
        site_id=proposal.site_id,
        decision=decision,
    )
    if block["state"] == STATE_DENIED:
        decision = DECISION_DENIED
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
        "approval": block,
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

    # E0.1: the configured policy decides how many approvers this needs
    # and who may be one. Raises ApprovalIncomplete when this approval is
    # valid but the subject is still short.
    block = await _record_and_evaluate(
        session,
        user=user,
        tenant_id=user.tenant_id,
        subject_type=SUBJECT_ACTION,
        subject_ref=action_id,
        action_type=route.action_type,
        device_agent_id=route.device_agent_id,
        site_id=route.site_id,
        decision=decision,
    )
    if block["state"] == STATE_DENIED:
        decision = DECISION_DENIED
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
        "origin": "node",
        "decision": decision,
        "decided_by": decided_by,
        "delivery": delivery,
        "approval": block,
        "route": _route_dict(route),
    }


async def _attach_approval_progress(
    session: AsyncSession, tenant_id: str, items: list[dict]
) -> None:
    """Add the `approval` block to each queue item. Two queries, not N."""
    if not items:
        return
    repo = ApprovalRecordRepo(session)
    by_subject: dict[str, list] = {}
    for subject_type in (SUBJECT_ACTION, SUBJECT_AGENT_PROPOSAL):
        refs = [
            i["action_id"] for i in items
            if (i["origin"] == "agent") == (subject_type == SUBJECT_AGENT_PROPOSAL)
        ]
        by_subject.update(await repo.map_for_subjects(subject_type, refs))

    policies = await ApprovalPolicyRepo(session).list_all(tenant_id)
    groups = {g.id: g for g in await ApprovalGroupRepo(session).list_all(tenant_id)}
    risks = action_risk_map()
    # The listing must resolve the SAME policy the decision will, or the
    # queue would promise one approver while the decision demands two.
    # One bulk read rather than a lookup per row.
    device_classes = {
        d.agent_id: (d.device_class or "")
        for d in await FleetCacheRepo(session).list_all(tenant_id)
    }
    for item in items:
        policy = resolve_policy(
            policies,
            action_type=item.get("action_type", ""),
            device_type=device_classes.get(item.get("device_agent_id", ""), ""),
            risk=risks.get((item.get("action_type") or "").upper(), ""),
        )
        group = groups.get(getattr(policy, "group_id", None) or "")
        item["approval"] = approval_block(
            policy, group, by_subject.get(item["action_id"], []),
        )


@router.get(
    "/",
    # A13/E0.3: an operator reads this because they work the queue;
    # an auditor reads it because it is the R-C3 evidence. Neither
    # may decide anything -- the POST routes below are unchanged.
    dependencies=[Depends(require_any_permission("action.approve", "audit.view"))],
)
async def list_pending(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    user: UserContext = Depends(
        require_any_permission("action.approve", "audit.view")
    ),
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

    # E0.1: every item says how many approvals it needs and how many it
    # has. An operator must be able to see "1 of 2" before deciding,
    # otherwise a second approver has no way to know they are needed.
    await _attach_approval_progress(session, user.tenant_id, items)
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
    """Record this approval; decide and dispatch when the policy is satisfied.

    Under a multi-approver policy the first approval is recorded and the
    action stays pending -- the response says how many more are needed,
    rather than reporting a success that did not happen.
    """
    try:
        return await _route_decision(action_id, DECISION_APPROVED, user, session, state)
    except ApprovalIncomplete as pending:
        return {
            "action_id": action_id,
            "decision": None,
            "recorded": True,
            "decided_by": user.email or user.user_id,
            "approval": pending.block,
            "detail": (
                f"approval recorded; {pending.block['remaining']} more "
                f"approver(s) required before this runs"
            ),
        }


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
    """Deny a pending action. A single denial is terminal (D16).

    Denial never waits for a quorum: an approver who objects cannot be
    outvoted by colleagues clicking faster.
    """
    return await _route_decision(action_id, DECISION_DENIED, user, session, state)


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
        except ApprovalIncomplete as pending:
            # Recorded, not yet decided. Reporting this as a failure would
            # be wrong; reporting it as done would be worse.
            results.append({
                "action_id": action_id,
                "ok": True,
                "pending": True,
                "detail": pending.block,
            })
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
    # A13/E0.3: an operator reads this because they work the queue;
    # an auditor reads it because it is the R-C3 evidence. Neither
    # may decide anything -- the POST routes below are unchanged.
    dependencies=[Depends(require_any_permission("action.approve", "audit.view"))],
)
async def approval_history(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    user: UserContext = Depends(
        require_any_permission("action.approve", "audit.view")
    ),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """History of approval decisions."""
    routes, total = await ApprovalRouteRepo(session).list_history_paginated(
        user.tenant_id, page=page, page_size=page_size,
    )
    items = [_route_dict(r) for r in routes]
    await _attach_approval_progress(session, user.tenant_id, items)
    return {
        "actions": items,
        "page": page,
        "page_size": page_size,
        "total": total,
        "tenant_id": user.tenant_id,
    }


@router.get(
    "/{action_id}/records",
    # A13/E0.3: an operator reads this because they work the queue;
    # an auditor reads it because it is the R-C3 evidence. Neither
    # may decide anything -- the POST routes below are unchanged.
    dependencies=[Depends(require_any_permission("action.approve", "audit.view"))],
)
async def approval_records(
    action_id: str,
    user: UserContext = Depends(
        require_any_permission("action.approve", "audit.view")
    ),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Every individual approval or denial recorded against this subject.

    The evidence R-C3 promises: not "who decided" but who each approver
    was, what they decided, and when. Works for both origins -- the
    subject is a node action id or an agent proposal id.
    """
    repo = ApprovalRecordRepo(session)
    records = list(await repo.list_for_subject(SUBJECT_ACTION, action_id))
    subject_type = SUBJECT_ACTION
    if not records:
        records = list(
            await repo.list_for_subject(SUBJECT_AGENT_PROPOSAL, action_id)
        )
        if records:
            subject_type = SUBJECT_AGENT_PROPOSAL
    # Tenant isolation: records carry their own tenant, so a subject from
    # another tenant reads as empty rather than leaking its approvers.
    records = [r for r in records if r.tenant_id == user.tenant_id]
    return {
        "subject_type": subject_type,
        "subject_ref": action_id,
        "tenant_id": user.tenant_id,
        "records": [
            {
                "approver": r.approver_email or r.approver_ref,
                "approver_ref": r.approver_ref,
                "decision": r.decision,
                "policy_id": r.policy_id or None,
                "scope_ok": r.scope_ok,
                "reason": r.reason,
                "decided_at": r.decided_at.isoformat() if r.decided_at else None,
            }
            for r in records
        ],
        "total": len(records),
    }
