"""Post-action verification (spec A2.1, A2.7 Outcome model).

After an action executes, the agent waits a verification window then
checks whether the action succeeded.  The result is captured as a
structured ActionOutcome for the learning pipeline.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from harkeniq.models import ActionType

logger = logging.getLogger("harkeniq.autonomy.verification")


class OutcomeStatus(str, Enum):
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILURE = "FAILURE"
    UNKNOWN = "UNKNOWN"
    ROLLBACK_TRIGGERED = "ROLLBACK_TRIGGERED"


@dataclass
class ActionOutcome:
    """Structured outcome of an executed action (A2.7 contract 1).

    This is the data model that the learning pipeline (P5) will consume.
    R3a captures outcomes; R3b/R4 learning algorithms read them.
    """

    action_id: str
    action_type: ActionType
    diagnosis_id: Optional[str] = None  # FK to Diagnosis (A2.7 contract 2)
    verification_ts: Optional[float] = None
    pre_state: dict[str, Any] = field(default_factory=dict)
    post_state: dict[str, Any] = field(default_factory=dict)
    outcome: OutcomeStatus = OutcomeStatus.UNKNOWN
    fault_resolved: Optional[bool] = None
    side_effects: list[str] = field(default_factory=list)
    operator_override: bool = False
    override_reason: str = ""


# Verification windows per action type (seconds to wait before checking)
VERIFICATION_WINDOWS = {
    ActionType.IDENTIFY_LED: 5,
    ActionType.COLLECT_DIAGNOSTICS: 5,
    ActionType.FAN_RESET: 60,
    ActionType.SEL_CLEAR: 30,
    ActionType.BMC_RESET: 120,
    ActionType.POWER_CYCLE: 300,
    ActionType.POWER_CAP_ADJUST: 30,
}


@dataclass
class VerificationCheck:
    """A single post-action verification check."""

    description: str
    field_path: str  # key in device state to check
    operator: str    # "equals", "greater_than", "less_than", "not_empty"
    expected: Any


# Verification checks per action type
VERIFICATION_CHECKS: dict[ActionType, list[VerificationCheck]] = {
    ActionType.SEL_CLEAR: [
        VerificationCheck(
            description="SEL accessible and empty",
            field_path="sel_entry_count",
            operator="equals",
            expected=0,
        ),
    ],
    ActionType.BMC_RESET: [
        VerificationCheck(
            description="BMC responds to Redfish",
            field_path="bmc_responsive",
            operator="equals",
            expected=True,
        ),
    ],
    ActionType.POWER_CYCLE: [
        VerificationCheck(
            description="Agent re-registered with SM",
            field_path="agent_registered",
            operator="equals",
            expected=True,
        ),
    ],
    ActionType.POWER_CAP_ADJUST: [
        VerificationCheck(
            description="Power draw within target range",
            field_path="power_within_target",
            operator="equals",
            expected=True,
        ),
    ],
    ActionType.FAN_RESET: [
        VerificationCheck(
            description="Fan RPM recovered above threshold",
            field_path="fan_rpm_healthy",
            operator="equals",
            expected=True,
        ),
    ],
}


def evaluate_verification(
    action_type: ActionType,
    post_state: dict[str, Any],
) -> OutcomeStatus:
    """Evaluate verification checks against post-action device state.

    Returns the outcome status based on check results.
    """
    checks = VERIFICATION_CHECKS.get(action_type, [])
    if not checks:
        return OutcomeStatus.SUCCESS  # no checks defined = assume success

    passed = 0
    for check in checks:
        value = post_state.get(check.field_path)
        if _check_passes(value, check.operator, check.expected):
            passed += 1

    if passed == len(checks):
        return OutcomeStatus.SUCCESS
    if passed > 0:
        return OutcomeStatus.PARTIAL
    return OutcomeStatus.FAILURE


def _check_passes(value: Any, operator: str, expected: Any) -> bool:
    if value is None:
        return False
    if operator == "equals":
        return value == expected
    if operator == "greater_than":
        return value > expected
    if operator == "less_than":
        return value < expected
    if operator == "not_empty":
        return bool(value)
    return False
