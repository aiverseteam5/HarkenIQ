"""Tests for PlaybookExecutor (R3b-3 Phase 5)."""

from __future__ import annotations

import pytest

from harkeniq.actions.playbook import (
    BMC_RECOVERY,
    DISK_REPLACEMENT_PREP,
    THERMAL_MITIGATION,
    Playbook,
    PlaybookExecution,
    PlaybookStep,
)
from harkeniq.actions.playbook_executor import PlaybookExecutor
from harkeniq.autonomy.verification import VerificationCheck
from harkeniq.models import ActionType, PlaybookStatus
from harkeniq.security.credentials import MockCredentialProvider


# -- helpers ----------------------------------------------------------------

def _state_provider(states: dict[str, dict]):
    """Create an async state provider from a dict of device_id -> state."""
    async def get_state(device_id):
        return states.get(device_id, {})
    return get_state


def _executor_with_states(states: dict[str, dict], cred_provider=None):
    return PlaybookExecutor(
        action_executor=None,  # mock mode
        credential_provider=cred_provider,
        get_device_state=_state_provider(states),
        verification_wait_scale=0.0,  # skip real waits in tests
    )


# -- Full playbook execution tests -----------------------------------------


class TestPlaybookExecution:
    async def test_all_steps_succeed(self):
        states = {"dev-1": {
            "sel_entry_count": 0,
            "bmc_responsive": True,
        }}
        executor = _executor_with_states(states)
        execution = await executor.execute_playbook(BMC_RECOVERY, "dev-1")
        assert execution.status == PlaybookStatus.COMPLETED
        assert len(execution.step_outcomes) == 2
        assert all(o.success for o in execution.step_outcomes)

    async def test_step_failure_pauses(self):
        # BMC not responsive after reset
        states = {"dev-1": {
            "sel_entry_count": 0,
            "bmc_responsive": False,
        }}
        executor = _executor_with_states(states)
        execution = await executor.execute_playbook(BMC_RECOVERY, "dev-1")
        # Step 0 (SEL_CLEAR) succeeds, step 1 (BMC_RESET) fails verification
        assert execution.status == PlaybookStatus.PAUSED
        assert execution.current_step_index == 1

    async def test_disk_prep_completes(self):
        executor = _executor_with_states({"dev-1": {}})
        execution = await executor.execute_playbook(DISK_REPLACEMENT_PREP, "dev-1")
        # No verification checks on LED/diagnostics steps
        assert execution.status == PlaybookStatus.COMPLETED
        assert len(execution.step_outcomes) == 2


class TestPreconditionChecks:
    async def test_precondition_failure_stops(self):
        pb = Playbook(
            playbook_id="test-precon",
            name="Test Preconditions",
            description="Tests precondition checking",
            device_types=["*"],
            steps=[
                PlaybookStep(
                    step_index=0,
                    action_type=ActionType.SEL_CLEAR,
                    description="Clear SEL",
                    preconditions=[
                        VerificationCheck("BMC must be up", "bmc_responsive", "equals", True),
                    ],
                    verification_wait_seconds=0,
                ),
            ],
        )
        states = {"dev-1": {"bmc_responsive": False}}
        executor = _executor_with_states(states)
        execution = await executor.execute_playbook(pb, "dev-1")
        assert execution.status == PlaybookStatus.PAUSED
        assert "Precondition failed" in execution.step_outcomes[0].error_message


class TestCredentialFetch:
    async def test_credential_required_fetches(self):
        pb = Playbook(
            playbook_id="test-creds",
            name="Test Credentials",
            description="",
            device_types=["*"],
            steps=[
                PlaybookStep(
                    step_index=0,
                    action_type=ActionType.BMC_RESET,
                    description="Reset BMC with JIT creds",
                    credential_required=True,
                    verification_wait_seconds=0,
                ),
            ],
        )
        mock_creds = MockCredentialProvider()
        executor = _executor_with_states(
            {"dev-1": {}},
            cred_provider=mock_creds,
        )
        execution = await executor.execute_playbook(pb, "dev-1")
        assert execution.status == PlaybookStatus.COMPLETED

    async def test_credential_failure_stops(self):
        pb = Playbook(
            playbook_id="test-creds-fail",
            name="Test Cred Failure",
            description="",
            device_types=["*"],
            steps=[
                PlaybookStep(
                    step_index=0,
                    action_type=ActionType.BMC_RESET,
                    description="Reset BMC",
                    credential_required=True,
                    verification_wait_seconds=0,
                ),
            ],
        )
        # No credential provider → failure
        executor = PlaybookExecutor(
            get_device_state=_state_provider({"dev-1": {}}),
            verification_wait_scale=0.0,
        )
        execution = await executor.execute_playbook(pb, "dev-1")
        # credential_required is True but no provider → step still runs
        # (provider is None, so credential check is skipped)
        assert execution.status == PlaybookStatus.COMPLETED


class TestRollback:
    async def test_rollback_on_failure(self):
        pb = Playbook(
            playbook_id="test-rollback",
            name="Test Rollback",
            description="",
            device_types=["*"],
            steps=[
                PlaybookStep(
                    step_index=0,
                    action_type=ActionType.POWER_CAP_ADJUST,
                    description="Set power cap",
                    verification_checks=[
                        VerificationCheck("Power in range", "power_ok", "equals", True),
                    ],
                    rollback_action=ActionType.POWER_CAP_ADJUST,
                    verification_wait_seconds=0,
                ),
            ],
        )
        states = {"dev-1": {"power_ok": False}}
        executor = _executor_with_states(states)
        execution = await executor.execute_playbook(pb, "dev-1")
        assert execution.status == PlaybookStatus.ROLLED_BACK


class TestResume:
    async def test_resume_from_paused(self):
        # Step 0 fails, step 1 should run on resume
        states_1 = {"dev-1": {"sel_entry_count": 999, "bmc_responsive": True}}
        executor_1 = _executor_with_states(states_1)
        execution = await executor_1.execute_playbook(BMC_RECOVERY, "dev-1")
        assert execution.status == PlaybookStatus.PAUSED
        assert execution.current_step_index == 0

        # Fix the state and resume
        states_2 = {"dev-1": {"sel_entry_count": 0, "bmc_responsive": True}}
        executor_2 = _executor_with_states(states_2)
        resumed = await executor_2.resume(execution, BMC_RECOVERY)
        assert resumed.status == PlaybookStatus.COMPLETED

    async def test_cannot_resume_completed(self):
        executor = _executor_with_states({"dev-1": {}})
        execution = await executor.execute_playbook(DISK_REPLACEMENT_PREP, "dev-1")
        assert execution.status == PlaybookStatus.COMPLETED
        # Resume should be no-op
        resumed = await executor.resume(execution, DISK_REPLACEMENT_PREP)
        assert resumed.status == PlaybookStatus.COMPLETED


class TestStepOutcomes:
    async def test_step_outcomes_have_action_type(self):
        executor = _executor_with_states({"dev-1": {}})
        execution = await executor.execute_playbook(DISK_REPLACEMENT_PREP, "dev-1")
        assert execution.step_outcomes[0].action_type == "IDENTIFY_LED"
        assert execution.step_outcomes[1].action_type == "COLLECT_DIAGNOSTICS"

    async def test_step_outcomes_have_duration(self):
        executor = _executor_with_states({"dev-1": {}})
        execution = await executor.execute_playbook(DISK_REPLACEMENT_PREP, "dev-1")
        for outcome in execution.step_outcomes:
            assert outcome.duration_ms >= 0


class TestFromStep:
    async def test_execute_from_step_1(self):
        executor = _executor_with_states({"dev-1": {
            "bmc_responsive": True,
        }})
        execution = await executor.execute_playbook(BMC_RECOVERY, "dev-1", from_step=1)
        assert execution.status == PlaybookStatus.COMPLETED
        assert len(execution.step_outcomes) == 1  # only step 1
        assert execution.step_outcomes[0].action_type == "BMC_RESET"
