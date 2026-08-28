"""Multi-step playbook model (R3b-3 Phase 3, spec doc 03).

A Playbook is a sequence of Steps.  Each Step has: action_type, params,
preconditions, verification_checks, optional rollback_action.  Playbook
execution tracks per-step outcomes with resume capability on partial failure.

Built-in playbooks from the spec:
  - BMC_RECOVERY: SEL clear → BMC reset → verify
  - THERMAL_MITIGATION: power cap → verify → fan reset
  - DISK_REPLACEMENT_PREP: identify LED → collect diagnostics
  - NIC_FAILOVER: (placeholder — needs bond detection)
"""

from __future__ import annotations

import dataclasses
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from harkeniq.autonomy.verification import VerificationCheck
from harkeniq.models import ActionType, PlaybookStatus


@dataclass
class PlaybookStep:
    """A single step in a playbook."""

    step_index: int
    action_type: ActionType
    description: str
    params: dict[str, str] = field(default_factory=dict)
    preconditions: list[VerificationCheck] = field(default_factory=list)
    verification_checks: list[VerificationCheck] = field(default_factory=list)
    rollback_action: Optional[ActionType] = None
    credential_required: bool = False
    verification_wait_seconds: float = 30.0


@dataclass
class StepOutcome:
    """Result of executing a single playbook step."""

    step_index: int
    action_type: str
    success: bool
    error_message: str = ""
    duration_ms: float = 0.0
    pre_state: dict[str, Any] = field(default_factory=dict)
    post_state: dict[str, Any] = field(default_factory=dict)
    rolled_back: bool = False
    timestamp: str = ""


@dataclass
class Playbook:
    """A named sequence of remediation steps."""

    playbook_id: str
    name: str
    description: str
    device_types: list[str]   # ["dell", "hpe"] or ["*"] for all
    steps: list[PlaybookStep]
    risk_level: str = "medium"  # "low" | "medium" | "high"

    @property
    def step_count(self) -> int:
        return len(self.steps)


@dataclass
class PlaybookExecution:
    """Tracks the state of a playbook execution."""

    execution_id: str
    playbook_id: str
    playbook_name: str
    device_id: str
    current_step_index: int = 0
    step_outcomes: list[StepOutcome] = field(default_factory=list)
    status: PlaybookStatus = PlaybookStatus.RUNNING
    started_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    error_message: str = ""

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            PlaybookStatus.COMPLETED,
            PlaybookStatus.FAILED,
            PlaybookStatus.ROLLED_BACK,
        )

    @property
    def progress(self) -> str:
        total = len(self.step_outcomes)
        return f"step {self.current_step_index + 1}/{total or '?'}"

    def record_step(self, outcome: StepOutcome) -> None:
        self.step_outcomes.append(outcome)
        if not outcome.success:
            if outcome.rolled_back:
                self.status = PlaybookStatus.ROLLED_BACK
            else:
                self.status = PlaybookStatus.PAUSED
            self.error_message = outcome.error_message

    def complete(self) -> None:
        self.status = PlaybookStatus.COMPLETED
        self.completed_at = time.time()

    def fail(self, reason: str) -> None:
        self.status = PlaybookStatus.FAILED
        self.error_message = reason
        self.completed_at = time.time()

    @staticmethod
    def create(playbook: Playbook, device_id: str) -> PlaybookExecution:
        return PlaybookExecution(
            execution_id=f"pb-{uuid.uuid4().hex[:8]}",
            playbook_id=playbook.playbook_id,
            playbook_name=playbook.name,
            device_id=device_id,
        )

    # QA-031: checkpoint serialization (crash-safe resume state)

    def to_dict(self) -> dict:
        data = dataclasses.asdict(self)
        data["status"] = self.status.value
        return data

    @staticmethod
    def from_dict(data: dict) -> PlaybookExecution:
        outcomes = [
            StepOutcome(**outcome) for outcome in data.get("step_outcomes", [])
        ]
        return PlaybookExecution(
            execution_id=data["execution_id"],
            playbook_id=data["playbook_id"],
            playbook_name=data.get("playbook_name", ""),
            device_id=data.get("device_id", ""),
            current_step_index=data.get("current_step_index", 0),
            step_outcomes=outcomes,
            status=PlaybookStatus(data["status"]),
            started_at=data.get("started_at", 0.0),
            completed_at=data.get("completed_at"),
            error_message=data.get("error_message", ""),
        )


# -- Built-in playbooks ----------------------------------------------------


BMC_RECOVERY = Playbook(
    playbook_id="builtin-bmc-recovery",
    name="BMC Recovery",
    description="Clear SEL, reset BMC, verify responsiveness",
    device_types=["*"],
    risk_level="medium",
    steps=[
        PlaybookStep(
            step_index=0,
            action_type=ActionType.SEL_CLEAR,
            description="Clear System Event Log to free BMC resources",
            verification_checks=[
                VerificationCheck("SEL accessible and empty", "sel_entry_count", "equals", 0),
            ],
            verification_wait_seconds=30.0,
        ),
        PlaybookStep(
            step_index=1,
            action_type=ActionType.BMC_RESET,
            description="Reset BMC to recover from hung state",
            verification_checks=[
                VerificationCheck("BMC responds to Redfish", "bmc_responsive", "equals", True),
            ],
            rollback_action=None,  # BMC reset has no rollback
            verification_wait_seconds=120.0,
        ),
    ],
)

THERMAL_MITIGATION = Playbook(
    playbook_id="builtin-thermal-mitigation",
    name="Thermal Mitigation",
    description="Apply power cap then reset fans to mitigate thermal event",
    device_types=["*"],
    risk_level="medium",
    steps=[
        PlaybookStep(
            step_index=0,
            action_type=ActionType.POWER_CAP_ADJUST,
            description="Reduce power consumption to lower thermal output",
            params={"target_watts": "400"},
            verification_checks=[
                VerificationCheck("Power within target", "power_within_target", "equals", True),
            ],
            verification_wait_seconds=30.0,
        ),
        PlaybookStep(
            step_index=1,
            action_type=ActionType.FAN_RESET,
            description="Reset fan control to optimal cooling profile",
            verification_checks=[
                VerificationCheck("Fan RPM recovered", "fan_rpm_healthy", "equals", True),
            ],
            verification_wait_seconds=60.0,
        ),
    ],
)

DISK_REPLACEMENT_PREP = Playbook(
    playbook_id="builtin-disk-replacement-prep",
    name="Disk Replacement Preparation",
    description="Identify failing drive and collect diagnostics for dispatch",
    device_types=["*"],
    risk_level="low",
    steps=[
        PlaybookStep(
            step_index=0,
            action_type=ActionType.IDENTIFY_LED,
            description="Blink LED on the failing drive for physical identification",
            verification_wait_seconds=5.0,
        ),
        PlaybookStep(
            step_index=1,
            action_type=ActionType.COLLECT_DIAGNOSTICS,
            description="Collect full system diagnostics for the dispatch ticket",
            verification_wait_seconds=5.0,
        ),
    ],
)


BUILTIN_PLAYBOOKS: dict[str, Playbook] = {
    BMC_RECOVERY.playbook_id: BMC_RECOVERY,
    THERMAL_MITIGATION.playbook_id: THERMAL_MITIGATION,
    DISK_REPLACEMENT_PREP.playbook_id: DISK_REPLACEMENT_PREP,
}
