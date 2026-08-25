"""Action approval endpoints (R-S6): list, approve, deny."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from harkeniq_sm.api.deps import require_site_token
from harkeniq_sm.approvals import ApprovalError
from harkeniq_sm.db.repos import ActionRepo, DeviceRepo

router = APIRouter(
    prefix="/api/actions", dependencies=[Depends(require_site_token)]
)


class Decision(BaseModel):
    """SM-local decision (QA-006).

    The SM's HTTP auth is a shared site token, so this actor is ASSERTED,
    not verified — it is recorded as ``sm-local:<actor>`` so the audit
    trail never mistakes it for an authenticated identity. The verified
    path is Central Command's /api/approvals (JWT-attributed, routed via
    RouteApproval). A default identity is refused: the operator must type
    who they are.
    """

    actor: str = Field(min_length=1)


def _action_dict(row, agent_id: str | None) -> dict:
    return {
        "id": row.id,
        "agent_id": agent_id,
        "agent_action_id": row.agent_action_id,
        "type": row.type,
        "sensor_id": row.sensor_id,
        "skill_name": row.skill_name,
        "verdict_severity": row.verdict_severity,
        "params": row.params,
        "status": row.status,
        "proposed_at": row.proposed_at,
        "decision": row.decision,
        "decided_by": row.decided_by,
        "decided_at": row.decided_at.isoformat() if row.decided_at else None,
        "outcome": row.outcome,
        "incident_id": row.incident_id,
    }


async def _with_agent_id(session, row) -> dict:
    device = await DeviceRepo(session).get(row.device_id)
    return _action_dict(row, device.agent_id if device else None)


def _raise(error: ApprovalError) -> None:
    status = 404 if error.code == "not_found" else 409
    raise HTTPException(status_code=status, detail=str(error))


@router.get("")
async def list_actions(request: Request) -> list[dict]:
    state = request.app.state.sm
    async with state.sessionmaker() as session:
        return [
            await _with_agent_id(session, row)
            for row in await ActionRepo(session).list_all()
        ]


@router.post("/{action_id}/approve")
async def approve(action_id: str, decision: Decision, request: Request) -> dict:
    state = request.app.state.sm
    try:
        row = await state.approvals.approve(
            action_id, actor=f"sm-local:{decision.actor}"
        )
    except ApprovalError as error:
        _raise(error)
    async with state.sessionmaker() as session:
        return await _with_agent_id(session, row)


@router.post("/{action_id}/deny")
async def deny(action_id: str, decision: Decision, request: Request) -> dict:
    state = request.app.state.sm
    try:
        row = await state.approvals.deny(
            action_id, actor=f"sm-local:{decision.actor}"
        )
    except ApprovalError as error:
        _raise(error)
    async with state.sessionmaker() as session:
        return await _with_agent_id(session, row)
