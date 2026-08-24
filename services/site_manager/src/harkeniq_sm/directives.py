"""SM->agent directed-directive service (R5 transport).

Agents dial out to the SM; the SM never dials agents. SM-initiated
work is queued as DirectedDirective rows and handed to the agent on
its PollDirectives call; the agent executes locally (its allow list
and audit still apply -- a directive is NOT a policy bypass) and
closes the loop with ReportDirectiveResult.

This is the transport R4-3 shipped its seams for: firmware campaigns
(AgentDirectedUpdater below implements the orchestrator's DeviceUpdater
over directives) and marketplace skill installs (kind=skill_install).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from harkeniq.proto import harkeniq_pb2
from harkeniq_sm.db.repos import AuditRepo, DeviceRepo, DirectiveRepo
from harkeniq_sm.firmware_orchestrator import UpdateResult

logger = logging.getLogger("harkeniq.sm.directives")


class DirectiveService:
    """Queue, deliver, and settle directed directives."""

    def __init__(self, sessionmaker, config) -> None:
        self._sessionmaker = sessionmaker
        self.config = config

    async def enqueue_action(
        self,
        device_id: str,
        action_type: str,
        params: Optional[dict] = None,
        issued_by: str = "",
    ) -> str:
        async with self._sessionmaker() as session:
            directive = await DirectiveRepo(session).enqueue(
                device_id=device_id, kind="action",
                action_type=action_type, params=params or {},
                issued_by=issued_by,
            )
            await AuditRepo(session).append(
                issued_by or "sm", "directive.enqueue", directive.id,
                detail={"kind": "action", "action_type": action_type,
                        "device_id": device_id},
            )
            await session.commit()
            return directive.id

    async def enqueue_skill_install(
        self,
        device_id: str,
        skill_id: str,
        skill_version: str,
        yaml_content: str,
        tier: str = "community",
        validation_state: str = "tested",
        issued_by: str = "marketplace",
    ) -> str:
        async with self._sessionmaker() as session:
            directive = await DirectiveRepo(session).enqueue(
                device_id=device_id, kind="skill_install",
                skill_id=skill_id, skill_version=skill_version,
                yaml_content=yaml_content, tier=tier,
                validation_state=validation_state, issued_by=issued_by,
            )
            await AuditRepo(session).append(
                issued_by or "sm", "directive.enqueue", directive.id,
                detail={"kind": "skill_install", "skill_id": skill_id,
                        "device_id": device_id},
            )
            await session.commit()
            return directive.id

    async def poll(self, agent_id: str) -> list:
        """Pending directives for the agent, marked delivered."""
        async with self._sessionmaker() as session:
            device = await DeviceRepo(session).get_by_agent_id(agent_id)
            if device is None:
                return []
            repo = DirectiveRepo(session)
            pending = await repo.pending_for_device(device.id)
            messages = []
            for directive in pending:
                await repo.mark_delivered(directive)
                messages.append(harkeniq_pb2.Directive(
                    directive_id=directive.id,
                    kind=directive.kind,
                    action_type=directive.action_type,
                    params_json=json.dumps(directive.params or {}),
                    skill_id=directive.skill_id,
                    skill_version=directive.skill_version,
                    yaml_content=directive.yaml_content or "",
                    tier=directive.tier,
                    validation_state=directive.validation_state,
                    issued_by=directive.issued_by,
                ))
            await session.commit()
            return messages

    async def report_result(
        self, agent_id: str, directive_id: str, success: bool, detail: str
    ) -> bool:
        async with self._sessionmaker() as session:
            repo = DirectiveRepo(session)
            directive = await repo.get(directive_id)
            if directive is None:
                return False
            device = await DeviceRepo(session).get_by_agent_id(agent_id)
            if device is None or directive.device_id != device.id:
                logger.warning(
                    "Directive %s result from wrong agent %s",
                    directive_id, agent_id,
                )
                return False
            if directive.status not in ("delivered", "pending"):
                return False  # already settled
            await repo.complete(directive_id, success, detail)
            await AuditRepo(session).append(
                f"agent:{agent_id}", "directive.result", directive_id,
                detail={"success": success, "detail": detail[:200]},
            )
            await session.commit()
            return True

    async def get_status(self, directive_id: str) -> tuple[str, str]:
        async with self._sessionmaker() as session:
            directive = await DirectiveRepo(session).get(directive_id)
            if directive is None:
                return "missing", ""
            return directive.status, directive.result_detail

    async def wait_for_completion(
        self,
        directive_id: str,
        timeout_s: float = 900.0,
        poll_interval_s: float = 2.0,
    ) -> tuple[str, str]:
        """Await settlement; a directive still open at the deadline is
        reported as timed_out (the row is left as-is for diagnosis)."""
        deadline = asyncio.get_event_loop().time() + timeout_s
        while True:
            status, detail = await self.get_status(directive_id)
            if status in ("completed", "failed", "missing"):
                return status, detail
            if asyncio.get_event_loop().time() >= deadline:
                return "timed_out", "agent did not settle the directive in time"
            await asyncio.sleep(poll_interval_s)


class AgentDirectedUpdater:
    """DeviceUpdater over the directive transport (firmware campaigns).

    The orchestrator's per-device update becomes: enqueue a
    FIRMWARE_UPDATE directive -> the agent polls, executes through its
    ActionExecutor (allow list, preconditions, audit), reports back ->
    we settle. Same for blue-green rollback.
    """

    def __init__(
        self,
        directives: DirectiveService,
        timeout_s: float = 900.0,
        poll_interval_s: float = 2.0,
    ) -> None:
        self._directives = directives
        self._timeout_s = timeout_s
        self._poll_interval_s = poll_interval_s

    async def update(self, device, campaign) -> UpdateResult:
        directive_id = await self._directives.enqueue_action(
            device.id, "FIRMWARE_UPDATE",
            params={
                "component": campaign.component,
                "target_version": campaign.target_version,
                "image_uri": campaign.image_uri,
            },
            issued_by=f"firmware-campaign:{campaign.id}",
        )
        status, detail = await self._directives.wait_for_completion(
            directive_id, self._timeout_s, self._poll_interval_s,
        )
        if status == "completed":
            return UpdateResult(
                success=True, post_version=campaign.target_version,
            )
        return UpdateResult(success=False, error=f"{status}: {detail}")

    async def rollback(self, device, campaign) -> bool:
        directive_id = await self._directives.enqueue_action(
            device.id, "FIRMWARE_ROLLBACK",
            params={"component": campaign.component},
            issued_by=f"firmware-campaign:{campaign.id}",
        )
        status, _ = await self._directives.wait_for_completion(
            directive_id, self._timeout_s, self._poll_interval_s,
        )
        return status == "completed"
