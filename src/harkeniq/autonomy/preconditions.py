"""Per-action precondition evaluation (spec A2.1).

Each R3a action has defined preconditions that must ALL pass before the
agent may execute.  Preconditions are evaluated locally by the agent
against current device state.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Optional

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


# ---------------------------------------------------------------------------
# Action reversibility (Capability Registry)
# ---------------------------------------------------------------------------
#
# The ONE genuinely new declaration the Capability Registry introduces, and
# it sits here beside ACTION_RISK deliberately: reversibility is a property
# of the action class itself, not of an agent, a device, or a page. Risk says
# how much a mistake costs; reversibility says whether a mistake can be
# undone, and they are not the same axis -- SEL_CLEAR is risk "low" and
# permanently destroys the event log, while POWER_CYCLE is risk "medium" and
# the device comes back to the state it was in.
#
# The Registry REPORTS this. It does not decide anything with it: no
# autonomy grant, no approval requirement and no execution gate reads it in
# this slice. It exists so an operator, and a future capability execution
# gate, can see the axis that ACTION_RISK alone never expressed.

#: No device state changes at all -- there is nothing to reverse.
REV_NONE = "none"
#: The device returns to its prior state on its own; no operator step.
REV_SELF_REVERTING = "self_reverting"
#: A governed action class restores the prior state. Named in INVERSE_ACTION.
REV_REVERSIBLE = "reversible"
#: Destroys state that cannot be recovered by any action in the platform.
REV_IRREVERSIBLE = "irreversible"

ACTION_REVERSIBILITY = {
    # Reads and identification
    ActionType.COLLECT_DIAGNOSTICS: REV_NONE,
    ActionType.IDENTIFY_LED: REV_REVERSIBLE,
    # Component restarts: the component comes back up by itself
    ActionType.FAN_RESET: REV_SELF_REVERTING,
    ActionType.BMC_RESET: REV_SELF_REVERTING,
    ActionType.POWER_CYCLE: REV_SELF_REVERTING,
    # Settings writes: the prior value is recorded and can be written back
    ActionType.POWER_CAP_ADJUST: REV_REVERSIBLE,
    ActionType.CONFIG_RESTORE: REV_REVERSIBLE,
    # Firmware: the standby bank holds the prior image (R4-3 blue-green)
    ActionType.FIRMWARE_UPDATE: REV_REVERSIBLE,
    ActionType.FIRMWARE_ROLLBACK: REV_REVERSIBLE,
    # Log and counter clears destroy the record itself. Nothing restores it.
    ActionType.SEL_CLEAR: REV_IRREVERSIBLE,
    ActionType.CLEAR_COUNTERS: REV_IRREVERSIBLE,
    # Network admin state
    ActionType.INTERFACE_DISABLE: REV_REVERSIBLE,
    ActionType.INTERFACE_ENABLE: REV_REVERSIBLE,
    # A reset bounces the link and it comes back; no persistent change.
    ActionType.INTERFACE_RESET: REV_SELF_REVERTING,
}

#: For REV_REVERSIBLE classes, the action class that restores the prior
#: state. Where an action is its own inverse (write the recorded prior
#: value back) it names itself, which is the honest answer rather than a
#: null that reads as "no way back".
INVERSE_ACTION = {
    ActionType.IDENTIFY_LED: ActionType.IDENTIFY_LED,
    ActionType.POWER_CAP_ADJUST: ActionType.POWER_CAP_ADJUST,
    ActionType.CONFIG_RESTORE: ActionType.CONFIG_RESTORE,
    ActionType.FIRMWARE_UPDATE: ActionType.FIRMWARE_ROLLBACK,
    ActionType.FIRMWARE_ROLLBACK: ActionType.FIRMWARE_UPDATE,
    ActionType.INTERFACE_DISABLE: ActionType.INTERFACE_ENABLE,
    ActionType.INTERFACE_ENABLE: ActionType.INTERFACE_DISABLE,
}


# ---------------------------------------------------------------------------
# Action parameter contract (spec A22.2)
# ---------------------------------------------------------------------------
#
# The missing half of the capability contract. The Registry could say an
# executor implements a class; nothing could say what that class REQUIRES
# in order to run. So A4 made five classes addressable whose executors
# demand a parameter the evaluator has never supplied -- every proposal
# carried params={"reason": ...} and was refused at the node.
#
# This sits beside ACTION_RISK and ACTION_REVERSIBILITY because it is the
# same kind of fact: a property of the action CLASS, true regardless of
# which agent asks, which device answers or which page renders it. Central
# Command, the Console, skills, the node and any future MCP consumer read
# it here. A second copy anywhere is a second answer.
#
# WHAT A SOURCE MEANS
# -------------------
# Declaring the parameter is not enough -- something has to be able to
# SUPPLY it truthfully. `source` records that, and `SRC_UNAVAILABLE` is a
# first-class answer: POWER_CAP_ADJUST needs a target wattage and this
# platform holds no power policy, so the honest report is "addressable,
# implemented, and not proposable here because nothing can say what the
# cap should be". Naming the missing input is the deliverable. Inventing
# one would be worse than the defect.

#: Resolvable from the affected component the Site Manager reports for an
#: incident -- the ``<component>`` half of a verdict's
#: ``"<subsystem>:<component>"`` sensor id (A22.4).
SRC_COMPONENT = "component"
#: The declaration itself carries a safe default; no caller input needed.
SRC_DEFAULT = "default"
#: Supplied by campaign orchestration (S6 / firmware), never by an agent
#: reacting to a fault (A21.10).
SRC_CAMPAIGN = "campaign"
#: Free-form context carried for audit and explanation. No executor reads
#: it, so it never gates execution and is accepted on every class.
SRC_ANNOTATION = "annotation"
#: Nothing in this platform can supply a truthful value yet. A class with
#: an unsatisfied required parameter of this source is reported, never
#: proposed, and never presented as executable (A22.5).
SRC_UNAVAILABLE = "unavailable"

PARAM_SOURCES = (
    SRC_COMPONENT, SRC_DEFAULT, SRC_CAMPAIGN, SRC_ANNOTATION, SRC_UNAVAILABLE,
)

#: Parameter value types. Deliberately three: the wire is JSON and the
#: executors read exactly these shapes. A richer type system here would be
#: a type system nobody asked for.
PTYPE_STRING = "string"
PTYPE_INTEGER = "integer"
PTYPE_JSON_OBJECT = "json_object"


@dataclass(frozen=True)
class ParamSpec:
    """One parameter of one action class."""

    name: str
    type: str
    required: bool
    source: str
    #: Present only when ``source`` is SRC_DEFAULT. The executor applies
    #: the same default; declaring it here lets a caller SEE it.
    default: Any = None
    #: Human-readable constraint, for the operator and the Console. Not
    #: machine-enforced beyond ``type`` -- a regex here would be a fourth
    #: place to get a device's identifier grammar wrong.
    constraint: str = ""
    #: Present only when ``source`` is SRC_UNAVAILABLE: what would have
    #: to exist for this parameter to be supplied.
    missing_input: str = ""


#: Accepted on every class, required by none. Existing skill YAML and
#: every node-proposed action already carry it; refusing it would break
#: the node funnel to satisfy a contract that does not read it.
REASON_PARAM = ParamSpec(
    name="reason", type=PTYPE_STRING, required=False, source=SRC_ANNOTATION,
    constraint="free-form explanation carried for audit; no executor reads it",
)

ACTION_PARAMETERS: dict[ActionType, tuple[ParamSpec, ...]] = {
    # -- no parameters: the class acts on the device as a whole ------------
    ActionType.COLLECT_DIAGNOSTICS: (),
    ActionType.SEL_CLEAR: (),
    ActionType.BMC_RESET: (),
    ActionType.FAN_RESET: (),

    # -- component-addressed: the affected component IS the parameter -----
    ActionType.IDENTIFY_LED: (
        ParamSpec(
            name="target", type=PTYPE_STRING, required=True,
            source=SRC_COMPONENT,
            constraint="a drive identifier the device's protocol exposes",
        ),
    ),
    ActionType.INTERFACE_DISABLE: (
        ParamSpec(
            name="interface", type=PTYPE_STRING, required=True,
            source=SRC_COMPONENT,
            constraint="a port name the device's protocol exposes",
        ),
    ),
    ActionType.INTERFACE_ENABLE: (
        ParamSpec(
            name="interface", type=PTYPE_STRING, required=True,
            source=SRC_COMPONENT,
            constraint="a port name the device's protocol exposes",
        ),
    ),

    # -- defaulted: the executor already has a safe answer ------------------
    ActionType.POWER_CYCLE: (
        ParamSpec(
            name="reset_type", type=PTYPE_STRING, required=False,
            source=SRC_DEFAULT, default="ForceRestart",
            constraint="a Redfish ResetType the device advertises",
        ),
    ),

    # -- unsatisfiable today, and said so out loud (A22.5) -----------------
    ActionType.POWER_CAP_ADJUST: (
        ParamSpec(
            name="target_watts", type=PTYPE_INTEGER, required=True,
            source=SRC_UNAVAILABLE,
            constraint="the chassis power cap to write, in watts",
            missing_input=(
                "no power policy exists in this platform, so nothing can "
                "say what the cap should be; a thermal incident does not "
                "imply a target wattage"
            ),
        ),
    ),
    ActionType.CONFIG_RESTORE: (
        ParamSpec(
            name="attributes_json", type=PTYPE_JSON_OBJECT, required=True,
            source=SRC_UNAVAILABLE,
            constraint="a non-empty object of attribute -> policy value",
            missing_input=(
                "drift detail is computed agent-side by the R4-2 compliance "
                "loop and never reaches Central Command; the drifted "
                "attributes and their policy values are not on the wire"
            ),
        ),
    ),

    # -- campaign-supplied: never proposed on a fault (A21.10) -------------
    ActionType.FIRMWARE_UPDATE: (
        ParamSpec(
            name="target_version", type=PTYPE_STRING, required=True,
            source=SRC_CAMPAIGN, constraint="the version the image installs",
        ),
        ParamSpec(
            name="image_uri", type=PTYPE_STRING, required=False,
            source=SRC_CAMPAIGN, constraint="where the node fetches the image",
        ),
        ParamSpec(
            name="component", type=PTYPE_STRING, required=False,
            source=SRC_DEFAULT, default="bmc",
            constraint="only 'bmc' is implemented (R4-3)",
        ),
    ),
    ActionType.FIRMWARE_ROLLBACK: (
        ParamSpec(
            name="component", type=PTYPE_STRING, required=False,
            source=SRC_DEFAULT, default="bmc",
            constraint="only 'bmc' is implemented (R4-3)",
        ),
        ParamSpec(
            name="expected_version", type=PTYPE_STRING, required=False,
            source=SRC_CAMPAIGN,
            constraint="verified against the standby bank after the swap",
        ),
    ),

    # -- unimplemented classes still declare, and stay honest (A21.9) ------
    #
    # No executor implements these on any protocol. Their parameter
    # contract is what it WOULD be, which is the same courtesy the
    # Registry already extends by naming them at all. Declaring nothing
    # here would make "unimplemented" and "takes no parameters"
    # indistinguishable.
    ActionType.CLEAR_COUNTERS: (
        ParamSpec(
            name="interface", type=PTYPE_STRING, required=True,
            source=SRC_COMPONENT,
            constraint="a port name the device's protocol exposes",
        ),
    ),
    ActionType.INTERFACE_RESET: (
        ParamSpec(
            name="interface", type=PTYPE_STRING, required=True,
            source=SRC_COMPONENT,
            constraint="a port name the device's protocol exposes",
        ),
    ),
}


def validate_param_names(
    action_type: ActionType, names: Iterable[str],
) -> tuple[bool, str]:
    """Are these the parameter names this class declares? (A22.2.)

    NAMES ONLY, deliberately. A skill's params are templates -- ``"{name}"``,
    ``"{life_left_pct}%"`` -- so a value cannot be type-checked until the
    substitution happens at the node. What IS statically true is which
    parameters exist and which are required, and that is exactly what a
    skill was previously free to get wrong: `skills/disk-health.yaml` has
    carried its own ``params: {target: "{name}"}`` block since R1, a fifth
    place the same fact was declared with nothing reconciling them.
    """
    specs = {s.name: s for s in ACTION_PARAMETERS.get(action_type, ())}
    specs[REASON_PARAM.name] = REASON_PARAM
    given = {str(n) for n in names}
    unknown = sorted(given - set(specs))
    if unknown:
        return False, (
            f"{action_type.value} does not declare "
            f"{', '.join(repr(u) for u in unknown)} "
            f"(declared: {', '.join(sorted(specs))})"
        )
    missing = sorted(
        name for name, spec in specs.items()
        if spec.required and name not in given
    )
    if missing:
        return False, (
            f"{action_type.value} requires "
            f"{', '.join(repr(m) for m in missing)}"
        )
    return True, ""
