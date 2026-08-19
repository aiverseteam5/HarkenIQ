"""Approval brokering (R-S6, D16): SM decides, agent applies.

First decider wins: a CLI approval/denial that reached the agent first
supersedes an undelivered SM decision. Denied is final. The wave guard
refuses to approve while another action in the same fault domain is
approved-but-undelivered or delivered-but-unfinished — never a whole
domain in one wave.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from harkeniq.proto import harkeniq_pb2
from harkeniq_sm.config import SMConfig
from harkeniq_sm.db.models import ActionRow
from harkeniq_sm.db.repos import (
    ActionRepo,
    AuditRepo,
    DeviceRepo,
    DomainRepo,
    IncidentRepo,
    SiteRepo,
)

logger = logging.getLogger("harkeniq.sm.approvals")

_IN_FLIGHT_STATUSES = ("approved", "delivered")


class ApprovalError(Exception):
    def __init__(self, message: str, code: str = "conflict") -> None:
        super().__init__(message)
        self.code = code  # "not_found" | "conflict"


def _loads(text: str) -> Optional[dict]:
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ApprovalService:
    def __init__(self, sessionmaker, config: SMConfig) -> None:
        self.sessionmaker = sessionmaker
        self.config = config

    # -- agent-facing (gRPC servicer delegates here) -------------------------

    async def report_action(self, request) -> bool:
        """Upsert an agent ActionReport; idempotent per (device, action_id)."""
        agent_status = request.status.upper()
        async with self.sessionmaker() as session:
            site = await SiteRepo(session).get_or_create(self.config.site_name)
            device = await DeviceRepo(session).upsert_registration(
                site_id=site.id, agent_id=request.agent_id
            )
            repo = ActionRepo(session)
            audit = AuditRepo(session)
            row = await repo.get_by_agent_action(device.id, request.action_id)
            outcome = _loads(request.outcome_json)
            if row is None:
                subsystem = request.sensor_id.split(":", 1)[0]
                incident = await IncidentRepo(session).open_device_incident(
                    device.id, subsystem
                )
                row = ActionRow(
                    device_id=device.id,
                    agent_action_id=request.action_id,
                    type=request.type,
                    sensor_id=request.sensor_id,
                    skill_name=request.skill_name,
                    verdict_severity=request.verdict_severity,
                    params=_loads(request.params_json),
                    proposed_at=request.proposed_at,
                    incident_id=incident.id if incident else None,
                    status=self._initial_status(agent_status),
                    outcome=outcome,
                )
                if row.status in ("delivered", "denied") and agent_status in (
                    "APPROVED", "DENIED"
                ):
                    row.decision = agent_status.lower()
                    row.decided_by = "agent-local"
                    row.decided_at = _utcnow()
                    row.delivered_at = _utcnow()
                session.add(row)
                await session.flush()
                await audit.append(
                    request.agent_id, "action.reported", row.id,
                    {"status": agent_status, "type": request.type},
                )
            else:
                self._apply_agent_status(row, agent_status, outcome)
                await audit.append(
                    request.agent_id, "action.status", row.id,
                    {"status": agent_status},
                )
            await session.commit()
        return True

    @staticmethod
    def _initial_status(agent_status: str) -> str:
        return {
            "PENDING": "pending",
            "APPROVED": "delivered",   # decided locally before SM ever saw it
            "DENIED": "denied",
            "EXECUTING": "delivered",
            "COMPLETED": "completed",
            "FAILED": "failed",
        }.get(agent_status, "pending")

    def _apply_agent_status(self, row: ActionRow, agent_status: str, outcome) -> None:
        now = _utcnow()
        if agent_status == "PENDING":
            return  # idempotent retry
        if agent_status in ("COMPLETED", "FAILED"):
            row.status = agent_status.lower()
            if outcome is not None:
                row.outcome = outcome
            return
        if agent_status == "EXECUTING":
            if row.status in ("approved", "delivered", "pending"):
                row.status = "delivered"
                row.delivered_at = row.delivered_at or now
            return
        if agent_status not in ("APPROVED", "DENIED"):
            return
        local_decision = agent_status.lower()
        if row.status == "pending":
            # CLI decided before the SM did — first decider wins.
            row.decision = local_decision
            row.decided_by = "agent-local"
            row.decided_at = now
            row.delivered_at = now
            row.status = "delivered" if local_decision == "approved" else "denied"
        elif row.status in ("approved", "denied"):
            if row.decision == local_decision:
                row.delivered_at = row.delivered_at or now
                if local_decision == "approved":
                    row.status = "delivered"
            else:
                # SM decision lost the race to a conflicting local decision.
                row.status = "superseded"
                row.delivered_at = row.delivered_at or now

    async def poll_decisions(self, agent_id: str) -> list:
        """Undelivered SM decisions for this agent; marks them delivered."""
        async with self.sessionmaker() as session:
            device = await DeviceRepo(session).get_by_agent_id(agent_id)
            if device is None:
                return []
            repo = ActionRepo(session)
            audit = AuditRepo(session)
            decisions = []
            for row in await repo.undelivered_decisions(device.id):
                row.delivered_at = _utcnow()
                if row.decision == "approved":
                    row.status = "delivered"
                decided_at = row.decided_at
                if decided_at is not None and decided_at.tzinfo is None:
                    decided_at = decided_at.replace(tzinfo=timezone.utc)
                decisions.append(
                    harkeniq_pb2.ActionDecision(
                        action_id=row.agent_action_id,
                        decision=row.decision or "",
                        decided_by=row.decided_by or "",
                        decided_at_unix=(
                            int(decided_at.timestamp()) if decided_at else 0
                        ),
                    )
                )
                await audit.append(
                    "site-manager", "action.delivered", row.id,
                    {"decision": row.decision, "agent_id": agent_id},
                )
            await session.commit()
        return decisions

    # -- operator-facing (HTTP API delegates here) ---------------------------

    async def approve(self, action_id: str, actor: str) -> ActionRow:
        async with self.sessionmaker() as session:
            repo = ActionRepo(session)
            row = await repo.get(action_id)
            if row is None:
                raise ApprovalError("unknown action", code="not_found")
            if row.status != "pending":
                raise ApprovalError(f"action is {row.status}, not pending")
            await self._wave_guard(session, row)
            row.status = "approved"
            row.decision = "approved"
            row.decided_by = actor
            row.decided_at = _utcnow()
            await AuditRepo(session).append(
                actor, "action.approve", row.id,
                {"agent_action_id": row.agent_action_id},
            )
            await session.commit()
            return row

    async def deny(self, action_id: str, actor: str) -> ActionRow:
        async with self.sessionmaker() as session:
            repo = ActionRepo(session)
            row = await repo.get(action_id)
            if row is None:
                raise ApprovalError("unknown action", code="not_found")
            if row.status != "pending":
                raise ApprovalError(f"action is {row.status}, not pending")
            row.status = "denied"
            row.decision = "denied"
            row.decided_by = actor
            row.decided_at = _utcnow()
            await AuditRepo(session).append(
                actor, "action.deny", row.id,
                {"agent_action_id": row.agent_action_id},
            )
            await session.commit()
            return row

    async def _wave_guard(self, session, row: ActionRow) -> None:
        """Refuse approval while the fault domain has an action in flight."""
        domain_repo = DomainRepo(session)
        member_ids: set[str] = set()
        for domain in await domain_repo.domains_for_device(row.device_id):
            member_ids.update(await domain_repo.members(domain.id))
        if not member_ids:
            return
        repo = ActionRepo(session)
        for status in _IN_FLIGHT_STATUSES:
            for other in await repo.list_by_status(status):
                if other.id != row.id and other.device_id in member_ids:
                    raise ApprovalError(
                        "another action in this fault domain is in flight "
                        f"({other.id}: {other.status})"
                    )
