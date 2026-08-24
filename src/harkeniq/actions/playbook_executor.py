"""Playbook executor — multi-step action orchestration (R3b-3 Phase 5).

Wraps ActionExecutor for sequential multi-step execution with:
  - Per-step precondition checking
  - Per-step verification (reuses VERIFICATION_WINDOWS)
  - Credential fetching for JIT-credential steps
  - Rollback on failure (if rollback_action defined)
  - Pause on partial failure (human review)
  - Resume capability from any step
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

from harkeniq.actions.playbook import (
    Playbook,
    PlaybookExecution,
    PlaybookStep,
    StepOutcome,
)
from harkeniq.autonomy.verification import (
    VerificationCheck,
    evaluate_verification,
)
from harkeniq.models import ActionType, PlaybookStatus

logger = logging.getLogger("harkeniq.actions.playbook_executor")


def _check_passes(value: Any, check: VerificationCheck) -> bool:
    """Evaluate a single verification/precondition check."""
    if value is None:
        return False
    if check.operator == "equals":
        return value == check.expected
    if check.operator == "greater_than":
        return value > check.expected
    if check.operator == "less_than":
        return value < check.expected
    if check.operator == "not_empty":
        return bool(value)
    return False


class PlaybookExecutor:
    """Orchestrates multi-step playbook execution.

    Each step runs through: precondition check → credential fetch →
    execute action → wait verification window → verify outcome.
    """

    def __init__(
        self,
        action_executor=None,
        credential_provider=None,
        get_device_state=None,
        verification_wait_scale: float = 1.0,
        dry_run: bool = False,
    ) -> None:
        """
        Args:
            action_executor: ActionExecutor instance (or mock for tests).
            credential_provider: CredentialProvider for JIT credentials.
            get_device_state: async callable(device_id) -> dict (current state).
            dry_run: log actions instead of executing them (R4-2 risk
                register: config-changing playbooks default to dry-run).
                Verification is skipped in dry-run -- nothing changed.
        """
        self._executor = action_executor
        self._cred_provider = credential_provider
        self._get_state = get_device_state
        self._wait_scale = verification_wait_scale  # 0.0 for tests
        self.dry_run = dry_run

    async def execute_playbook(
        self,
        playbook: Playbook,
        device_id: str,
        from_step: int = 0,
    ) -> PlaybookExecution:
        """Execute a playbook from a given step.

        Returns a PlaybookExecution with per-step outcomes.
        Terminal status: COMPLETED, FAILED, PAUSED, or ROLLED_BACK.
        """
        execution = PlaybookExecution.create(playbook, device_id)
        execution.current_step_index = from_step

        logger.info(
            "Starting playbook %s on %s (steps %d-%d)",
            playbook.name, device_id, from_step, playbook.step_count - 1,
        )

        for i in range(from_step, playbook.step_count):
            step = playbook.steps[i]
            execution.current_step_index = i

            outcome = await self._execute_step(step, device_id)
            execution.record_step(outcome)

            if not outcome.success:
                # Try rollback if defined
                if step.rollback_action and not outcome.rolled_back:
                    rollback_ok = await self._execute_rollback(
                        step.rollback_action, device_id,
                    )
                    if rollback_ok:
                        outcome.rolled_back = True
                        execution.status = PlaybookStatus.ROLLED_BACK
                    else:
                        execution.fail(
                            f"Step {i} failed and rollback failed: {outcome.error_message}"
                        )
                # Pause for human review (no rollback or rollback succeeded)
                if execution.status == PlaybookStatus.PAUSED:
                    logger.warning(
                        "Playbook %s paused at step %d: %s",
                        playbook.name, i, outcome.error_message,
                    )
                break
        else:
            # All steps completed successfully
            execution.complete()
            logger.info(
                "Playbook %s completed on %s (%d steps)",
                playbook.name, device_id, playbook.step_count,
            )

        return execution

    async def resume(
        self,
        execution: PlaybookExecution,
        playbook: Playbook,
    ) -> PlaybookExecution:
        """Resume a paused playbook from the next step."""
        if execution.status != PlaybookStatus.PAUSED:
            logger.warning("Cannot resume: status is %s", execution.status)
            return execution

        next_step = execution.current_step_index + 1
        if next_step >= playbook.step_count:
            execution.complete()
            return execution

        execution.status = PlaybookStatus.RUNNING
        resumed = await self.execute_playbook(
            playbook, execution.device_id, from_step=next_step,
        )
        # Merge outcomes
        for outcome in resumed.step_outcomes:
            execution.record_step(outcome)
        if resumed.status == PlaybookStatus.COMPLETED:
            execution.complete()
        else:
            execution.status = resumed.status
            execution.error_message = resumed.error_message
        return execution

    async def _execute_step(
        self,
        step: PlaybookStep,
        device_id: str,
    ) -> StepOutcome:
        """Execute a single playbook step."""
        started = time.time()
        iso_now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # 1. Check preconditions
        if step.preconditions:
            pre_state = await self._get_current_state(device_id)
            for check in step.preconditions:
                value = pre_state.get(check.field_path)
                if not _check_passes(value, check):
                    return StepOutcome(
                        step_index=step.step_index,
                        action_type=step.action_type.value,
                        success=False,
                        error_message=f"Precondition failed: {check.description}",
                        pre_state=pre_state,
                        timestamp=iso_now,
                    )

        # 2. Fetch credentials if needed
        if step.credential_required and self._cred_provider:
            cred = await self._cred_provider.get_credentials(device_id)
            if cred is None:
                return StepOutcome(
                    step_index=step.step_index,
                    action_type=step.action_type.value,
                    success=False,
                    error_message="Failed to fetch JIT credentials",
                    timestamp=iso_now,
                )

        # 3. Execute the action
        pre_state = await self._get_current_state(device_id)
        try:
            action_outcome = await self._run_action(step)
            if action_outcome is None or not action_outcome.get("success", False):
                error = action_outcome.get("error", "Action failed") if action_outcome else "No executor"
                return StepOutcome(
                    step_index=step.step_index,
                    action_type=step.action_type.value,
                    success=False,
                    error_message=str(error),
                    pre_state=pre_state,
                    duration_ms=(time.time() - started) * 1000,
                    timestamp=iso_now,
                )
        except Exception as e:
            return StepOutcome(
                step_index=step.step_index,
                action_type=step.action_type.value,
                success=False,
                error_message=str(e),
                pre_state=pre_state,
                duration_ms=(time.time() - started) * 1000,
                timestamp=iso_now,
            )

        # 4. Wait verification window (scaled for tests)
        wait = step.verification_wait_seconds * self._wait_scale
        if wait > 0 and not self.dry_run:
            await asyncio.sleep(wait)

        # 5. Verify outcome (skipped in dry-run: nothing was changed)
        post_state = await self._get_current_state(device_id)
        if step.verification_checks and not self.dry_run:
            all_pass = True
            for check in step.verification_checks:
                value = post_state.get(check.field_path)
                if not _check_passes(value, check):
                    all_pass = False
                    break
            if not all_pass:
                return StepOutcome(
                    step_index=step.step_index,
                    action_type=step.action_type.value,
                    success=False,
                    error_message="Verification failed after action",
                    pre_state=pre_state,
                    post_state=post_state,
                    duration_ms=(time.time() - started) * 1000,
                    timestamp=iso_now,
                )

        return StepOutcome(
            step_index=step.step_index,
            action_type=step.action_type.value,
            success=True,
            pre_state=pre_state,
            post_state=post_state,
            duration_ms=(time.time() - started) * 1000,
            timestamp=iso_now,
        )

    async def _run_action(self, step: PlaybookStep) -> Optional[dict]:
        """Run a single action via the ActionExecutor (R4-2: wired for real).

        The ActionExecutor's allow list and audit trail apply to playbook
        steps exactly as to directly approved actions -- a playbook is
        not a policy bypass.
        """
        if self._executor is None:
            return {"success": True}  # mock mode (tests without executor)
        if self.dry_run:
            logger.info(
                "[dry-run] would execute %s with params %s",
                step.action_type.value, dict(step.params),
            )
            return {"success": True, "dry_run": True}
        action = self._build_action(step.action_type, dict(step.params))
        outcome = await self._executor.execute(action)
        return {"success": outcome.success, "error": outcome.error_message or ""}

    async def _execute_rollback(
        self, rollback_action: ActionType, device_id: str,
    ) -> bool:
        """Execute a rollback action. Returns True on success."""
        logger.info("Executing rollback %s on %s", rollback_action.value, device_id)
        if self._executor is None:
            return True  # mock mode
        if self.dry_run:
            logger.info("[dry-run] would roll back via %s", rollback_action.value)
            return True
        action = self._build_action(rollback_action, {})
        outcome = await self._executor.execute(action)
        return outcome.success

    @staticmethod
    def _build_action(action_type: ActionType, params: dict):
        import uuid

        from harkeniq.models import Action

        return Action(
            id=f"pb-{uuid.uuid4().hex[:8]}",
            type=action_type,
            params=params,
            skill_name="playbook",
        )

    async def _get_current_state(self, device_id: str) -> dict:
        """Get current device state for verification."""
        if self._get_state:
            return await self._get_state(device_id)
        return {}
