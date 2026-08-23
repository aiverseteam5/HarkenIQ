"""Per-action precondition evaluation (spec A2.1).

Each R3a action has defined preconditions that must ALL pass before the
agent may execute.  Preconditions are evaluated locally by the agent
against current device state.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from harkeniq.models import ActionType

logger = logging.getLogger("harkeniq.autonomy.preconditions")


@dataclass
class PreconditionResult:
    """Result of evaluating preconditions for an action."""

    passed: bool
    action_type: ActionType
    failed_checks: list[str]  # human-readable reasons for failure

    @property
    def reason(self) -> str:
        return "; ".join(self.failed_checks) if self.failed_checks else "all checks passed"


def check_preconditions(
    action_type: ActionType,
    device_state: dict[str, Any],
    agent_state: dict[str, Any],
) -> PreconditionResult:
    """Evaluate preconditions for the given action type.

    Args:
        action_type: The action to check.
        device_state: Current device readings (health_summary, sensor data).
        agent_state: Agent internal state (peer count, SM contact, poll counts).

    Returns:
        PreconditionResult with pass/fail and reasons.
    """
    checker = _PRECONDITION_MAP.get(action_type)
    if checker is None:
        # R1 actions have no preconditions beyond the existing allow-list
        return PreconditionResult(passed=True, action_type=action_type, failed_checks=[])
    return checker(action_type, device_state, agent_state)


# ---------------------------------------------------------------------------
# Per-action precondition checkers
# ---------------------------------------------------------------------------


def _check_sel_clear(
    action_type: ActionType, device: dict, agent: dict
) -> PreconditionResult:
    """SEL clear: events must be forwarded to SM; SEL >80% full."""
    failures = []
    sel_forwarded = agent.get("sel_events_forwarded", False)
    if not sel_forwarded:
        failures.append("SEL events not yet forwarded to Site Manager")

    sel_pct_full = device.get("sel_percent_full", 0)
    if sel_pct_full < 80:
        failures.append(f"SEL only {sel_pct_full}% full (threshold: 80%)")

    return PreconditionResult(
        passed=len(failures) == 0,
        action_type=action_type,
        failed_checks=failures,
    )


def _check_bmc_reset(
    action_type: ActionType, device: dict, agent: dict
) -> PreconditionResult:
    """BMC reset: BMC unresponsive 3 polls; no in-flight firmware update."""
    failures = []
    consecutive_failures = agent.get("bmc_consecutive_poll_failures", 0)
    if consecutive_failures < 3:
        failures.append(
            f"BMC responsive (only {consecutive_failures} consecutive poll failures, need 3)"
        )

    firmware_updating = device.get("firmware_update_in_progress", False)
    if firmware_updating:
        failures.append("Firmware update in progress")

    return PreconditionResult(
        passed=len(failures) == 0,
        action_type=action_type,
        failed_checks=failures,
    )


def _check_power_cycle(
    action_type: ActionType, device: dict, agent: dict
) -> PreconditionResult:
    """Power cycle: T1 corroboration; OS heartbeat absent >5min."""
    failures = []
    alive_peers = agent.get("alive_peer_count", 0)
    if alive_peers < 2:
        failures.append(
            f"T1 corroboration required (only {alive_peers} alive peers, need >= 2)"
        )

    os_heartbeat_absent_s = agent.get("os_heartbeat_absent_seconds", 0)
    if os_heartbeat_absent_s < 300:
        failures.append(
            f"OS heartbeat still active ({os_heartbeat_absent_s}s absent, need >= 300s)"
        )

    return PreconditionResult(
        passed=len(failures) == 0,
        action_type=action_type,
        failed_checks=failures,
    )


def _check_power_cap_adjust(
    action_type: ActionType, device: dict, agent: dict
) -> PreconditionResult:
    """Power cap adjust: active thermal/power event; target within policy range."""
    failures = []
    thermal_event = device.get("thermal_event_active", False)
    power_event = device.get("power_event_active", False)
    if not thermal_event and not power_event:
        failures.append("No active thermal or power event")

    target_watts = device.get("power_cap_target_watts")
    policy_min = device.get("power_cap_policy_min_watts", 0)
    policy_max = device.get("power_cap_policy_max_watts", 0)
    if target_watts is not None and policy_max > 0:
        if target_watts < policy_min or target_watts > policy_max:
            failures.append(
                f"Target {target_watts}W outside policy range [{policy_min}-{policy_max}W]"
            )

    return PreconditionResult(
        passed=len(failures) == 0,
        action_type=action_type,
        failed_checks=failures,
    )


_PRECONDITION_MAP = {
    ActionType.SEL_CLEAR: _check_sel_clear,
    ActionType.BMC_RESET: _check_bmc_reset,
    ActionType.POWER_CYCLE: _check_power_cycle,
    ActionType.POWER_CAP_ADJUST: _check_power_cap_adjust,
}


# ---------------------------------------------------------------------------
# Action risk classification (A2.1)
# ---------------------------------------------------------------------------

ACTION_RISK = {
    ActionType.IDENTIFY_LED: "none",
    ActionType.COLLECT_DIAGNOSTICS: "none",
    ActionType.FAN_RESET: "low",
    ActionType.SEL_CLEAR: "low",
    ActionType.BMC_RESET: "low",
    ActionType.POWER_CYCLE: "medium",
    ActionType.POWER_CAP_ADJUST: "medium",
}
