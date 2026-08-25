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


def _check_firmware_update(
    action_type: ActionType, device: dict, agent: dict
) -> PreconditionResult:
    """Firmware update (R4-3): healthy device, no update already running.

    A firmware write on an already-degraded device compounds risk; the
    campaign should update healthy devices and let incidents resolve
    first. This is deliberately stricter than every other action.
    """
    failures = []
    health = str(device.get("overall_health", "")).lower()
    if health not in ("ok", "healthy"):
        failures.append(
            f"Device health must be OK before a firmware update (is {health or 'unknown'!r})"
        )
    if device.get("firmware_update_in_progress", False):
        failures.append("Another firmware update is already in progress")
    return PreconditionResult(
        passed=len(failures) == 0,
        action_type=action_type,
        failed_checks=failures,
    )


# ---------------------------------------------------------------------------
# R6 network action preconditions (A9 D6; design doc §7 decisions 4, 9, 10)
# ---------------------------------------------------------------------------


def _network_disruptive_common(device: dict, agent: dict) -> list[str]:
    """Checks shared by INTERFACE_RESET and INTERFACE_DISABLE.

    Fail-closed throughout: a missing input is a failed check, never a
    waved-through one.
    """
    failures = []
    target = device.get("target_interface")
    if not target:
        return ["no target interface specified"]
    interfaces: dict = device.get("interfaces") or {}
    iface = interfaces.get(target)
    if iface is None:
        return [f"target interface {target!r} not in current device state"]

    # Self-preservation (review 3A): the resolved management-path set.
    # Key ABSENT = resolution failed = cannot prove safety = refuse.
    mgmt = agent.get("mgmt_interfaces")
    if mgmt is None:
        failures.append(
            "management path could not be resolved — refusing (fail-closed): "
            "an action that cannot be proven safe is not safe"
        )
    elif target in mgmt or (iface.get("lag_name") or "") in mgmt:
        failures.append(
            f"self-preservation: {target} carries this agent's own "
            "management path to the Site Manager"
        )

    # Redundant path (review T6). Local leg: another oper-Up member of the
    # same LAG. Cross-device leg: the SM verifies against the site model at
    # approval time and stamps sm_redundancy_verified into the approval.
    lag = iface.get("lag_name")
    lag_redundant = lag is not None and any(
        other.get("lag_name") == lag and other.get("oper_state") == "Up"
        for name, other in interfaces.items() if name != target
    )
    if not lag_redundant and not agent.get("sm_redundancy_verified", False):
        failures.append(
            "redundant path unverifiable: no oper-Up LAG sibling locally "
            "and no SM site-model verification in the approval"
        )

    # T1 quorum corroboration gate (decision 9): >= 2 peers whose evidence
    # is consistent with the diagnosis. Degraded topology -> propose-only.
    corroborating = agent.get("corroborating_peers", 0)
    if corroborating < 2:
        failures.append(
            f"T1 quorum: only {corroborating} corroborating peers (need >= 2) "
            "— propose-only"
        )

    # Fault-domain blast radius (decision 10): never two ports of one LAG,
    # one disruptive action per switch domain per window.
    tracker = agent.get("network_tracker")
    if tracker is None:
        failures.append("no fault-domain tracker — refusing (fail-closed)")
    else:
        allowed, reason = tracker.allows(target, lag)
        if not allowed:
            failures.append(reason)
    return failures


def _check_interface_disable(
    action_type: ActionType, device: dict, agent: dict
) -> PreconditionResult:
    """Disable additionally requires a confident hardware-degradation
    diagnosis (A9 D6): confidence >= 0.8 AND classification is hardware,
    never load-correlated congestion (R-M5)."""
    failures = _network_disruptive_common(device, agent)
    confidence = device.get("diagnosis_confidence", 0.0)
    if confidence < 0.8:
        failures.append(
            f"diagnosis confidence {confidence:.2f} below 0.8 disable floor"
        )
    classification = device.get("diagnosis_classification", "")
    if classification != "hardware_degradation":
        failures.append(
            f"diagnosis classification {classification or 'unknown'!r} is not "
            "hardware_degradation — congestion is never disabled away (R-M5)"
        )
    return PreconditionResult(
        passed=len(failures) == 0, action_type=action_type,
        failed_checks=failures,
    )


def _check_interface_reset(
    action_type: ActionType, device: dict, agent: dict
) -> PreconditionResult:
    failures = _network_disruptive_common(device, agent)
    return PreconditionResult(
        passed=len(failures) == 0, action_type=action_type,
        failed_checks=failures,
    )


def _check_interface_enable(
    action_type: ActionType, device: dict, agent: dict
) -> PreconditionResult:
    """Enable is LOW risk only as a restore (review 7A): a recorded
    HarkenIQ pre-state must exist for the port. An arbitrary enable fails
    here and travels the HIGH approval path instead — re-energizing a port
    a human deliberately shut is a real operational landmine."""
    failures = []
    target = device.get("target_interface")
    if not target:
        failures.append("no target interface specified")
    elif not device.get("prestate_exists", False):
        failures.append(
            f"no recorded HarkenIQ pre-state for {target}: arbitrary enable "
            "classifies HIGH and requires full approval"
        )
    return PreconditionResult(
        passed=len(failures) == 0, action_type=action_type,
        failed_checks=failures,
    )


def _check_clear_counters(
    action_type: ActionType, device: dict, agent: dict
) -> PreconditionResult:
    """Counters must be snapshotted pre-clear (A9 D6) and the trending
    engine notified (design decision 7) — a zeroed counter must never read
    as recovery."""
    failures = []
    if not device.get("counters_snapshot_recorded", False):
        failures.append("pre-clear counter snapshot not recorded")
    return PreconditionResult(
        passed=len(failures) == 0, action_type=action_type,
        failed_checks=failures,
    )


_PRECONDITION_MAP = {
    ActionType.SEL_CLEAR: _check_sel_clear,
    ActionType.BMC_RESET: _check_bmc_reset,
    ActionType.POWER_CYCLE: _check_power_cycle,
    ActionType.POWER_CAP_ADJUST: _check_power_cap_adjust,
    ActionType.FIRMWARE_UPDATE: _check_firmware_update,
    ActionType.INTERFACE_DISABLE: _check_interface_disable,
    ActionType.INTERFACE_RESET: _check_interface_reset,
    ActionType.INTERFACE_ENABLE: _check_interface_enable,
    ActionType.CLEAR_COUNTERS: _check_clear_counters,
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
    ActionType.CONFIG_RESTORE: "medium",
    # R4-3: bricked device = permanent loss; highest risk class in the platform
    ActionType.FIRMWARE_UPDATE: "high",
    ActionType.FIRMWARE_ROLLBACK: "high",
    # R6 network actions (A9 D6). ENABLE is "low" only because its
    # precondition demands restore semantics (recorded pre-state); an
    # arbitrary enable fails the precondition and rides the approval path.
    ActionType.CLEAR_COUNTERS: "low",
    ActionType.INTERFACE_RESET: "high",
    ActionType.INTERFACE_DISABLE: "high",
    ActionType.INTERFACE_ENABLE: "low",
}
