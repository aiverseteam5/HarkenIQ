"""Tests for Playbook data model (R3b-3 Phase 3)."""

from __future__ import annotations

import pytest

from harkeniq.actions.playbook import (
    BMC_RECOVERY,
    BUILTIN_PLAYBOOKS,
    DISK_REPLACEMENT_PREP,
    THERMAL_MITIGATION,
    Playbook,
    PlaybookExecution,
    PlaybookStep,
    StepOutcome,
)
from harkeniq.autonomy.verification import VerificationCheck
from harkeniq.models import ActionType, PlaybookStatus


class TestPlaybookStep:
    def test_step_fields(self):
        step = PlaybookStep(
            step_index=0,
            action_type=ActionType.SEL_CLEAR,
            description="Clear SEL",
        )
        assert step.step_index == 0
        assert step.action_type == ActionType.SEL_CLEAR
        assert step.credential_required is False

    def test_step_with_verification(self):
        step = PlaybookStep(
            step_index=0,
            action_type=ActionType.BMC_RESET,
            description="Reset BMC",
            verification_checks=[
                VerificationCheck("BMC responds", "bmc_responsive", "equals", True),
            ],
        )
        assert len(step.verification_checks) == 1

    def test_step_with_rollback(self):
        step = PlaybookStep(
            step_index=0,
            action_type=ActionType.POWER_CAP_ADJUST,
            description="Set power cap",
            rollback_action=ActionType.POWER_CAP_ADJUST,
        )
        assert step.rollback_action == ActionType.POWER_CAP_ADJUST


class TestPlaybook:
    def test_playbook_step_count(self):
        pb = Playbook(
            playbook_id="test-pb",
            name="Test",
            description="Test playbook",
            device_types=["*"],
            steps=[
                PlaybookStep(0, ActionType.SEL_CLEAR, "step 1"),
                PlaybookStep(1, ActionType.BMC_RESET, "step 2"),
            ],
        )
        assert pb.step_count == 2

    def test_playbook_risk_level(self):
        pb = Playbook(
            playbook_id="test",
            name="Test",
            description="",
            device_types=["dell"],
            steps=[],
            risk_level="high",
        )
        assert pb.risk_level == "high"


class TestPlaybookExecution:
    def test_create_from_playbook(self):
        exe = PlaybookExecution.create(BMC_RECOVERY, "device-x")
        assert exe.playbook_id == "builtin-bmc-recovery"
        assert exe.device_id == "device-x"
        assert exe.status == PlaybookStatus.RUNNING
        assert exe.execution_id.startswith("pb-")

    def test_record_successful_step(self):
        exe = PlaybookExecution.create(BMC_RECOVERY, "device-x")
        outcome = StepOutcome(
            step_index=0,
            action_type="SEL_CLEAR",
            success=True,
            duration_ms=500.0,
        )
        exe.record_step(outcome)
        assert len(exe.step_outcomes) == 1
        assert exe.status == PlaybookStatus.RUNNING  # continues

    def test_record_failed_step_pauses(self):
        exe = PlaybookExecution.create(BMC_RECOVERY, "device-x")
        outcome = StepOutcome(
            step_index=0,
            action_type="SEL_CLEAR",
            success=False,
            error_message="SEL not accessible",
        )
        exe.record_step(outcome)
        assert exe.status == PlaybookStatus.PAUSED
        assert exe.error_message == "SEL not accessible"

    def test_record_rolled_back_step(self):
        exe = PlaybookExecution.create(BMC_RECOVERY, "device-x")
        outcome = StepOutcome(
            step_index=0,
            action_type="SEL_CLEAR",
            success=False,
            rolled_back=True,
        )
        exe.record_step(outcome)
        assert exe.status == PlaybookStatus.ROLLED_BACK

    def test_complete(self):
        exe = PlaybookExecution.create(BMC_RECOVERY, "device-x")
        exe.complete()
        assert exe.status == PlaybookStatus.COMPLETED
        assert exe.completed_at is not None
        assert exe.is_terminal is True

    def test_fail(self):
        exe = PlaybookExecution.create(BMC_RECOVERY, "device-x")
        exe.fail("precondition not met")
        assert exe.status == PlaybookStatus.FAILED
        assert exe.is_terminal is True

    def test_progress_string(self):
        exe = PlaybookExecution.create(BMC_RECOVERY, "device-x")
        assert "step 1" in exe.progress


class TestBuiltinPlaybooks:
    def test_bmc_recovery_structure(self):
        pb = BMC_RECOVERY
        assert pb.name == "BMC Recovery"
        assert pb.step_count == 2
        assert pb.steps[0].action_type == ActionType.SEL_CLEAR
        assert pb.steps[1].action_type == ActionType.BMC_RESET
        assert len(pb.steps[1].verification_checks) == 1

    def test_thermal_mitigation_structure(self):
        pb = THERMAL_MITIGATION
        assert pb.name == "Thermal Mitigation"
        assert pb.step_count == 2
        assert pb.steps[0].action_type == ActionType.POWER_CAP_ADJUST
        assert pb.steps[1].action_type == ActionType.FAN_RESET

    def test_disk_replacement_prep_low_risk(self):
        pb = DISK_REPLACEMENT_PREP
        assert pb.risk_level == "low"
        assert pb.step_count == 2
        assert pb.steps[0].action_type == ActionType.IDENTIFY_LED

    def test_all_builtins_registered(self):
        assert len(BUILTIN_PLAYBOOKS) == 3
        assert "builtin-bmc-recovery" in BUILTIN_PLAYBOOKS
        assert "builtin-thermal-mitigation" in BUILTIN_PLAYBOOKS
        assert "builtin-disk-replacement-prep" in BUILTIN_PLAYBOOKS

    def test_all_builtins_have_device_types(self):
        for pb in BUILTIN_PLAYBOOKS.values():
            assert len(pb.device_types) > 0

    def test_all_steps_have_descriptions(self):
        for pb in BUILTIN_PLAYBOOKS.values():
            for step in pb.steps:
                assert step.description, f"{pb.name} step {step.step_index} missing description"
