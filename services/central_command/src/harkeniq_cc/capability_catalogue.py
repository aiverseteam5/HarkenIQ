"""A4: the condition -> capability catalogue (spec A21).

WHAT THIS ANSWERS, AND WHAT IT DOES NOT
---------------------------------------
One question: *which capability is a candidate for which observed
condition.*

It is NOT a second capability-authority model (A21.2). The Capability
Registry remains the only authority on whether an executor can perform
an action, and this catalogue can never contradict it: an entry naming a
class no executor implements is refused on write and inert on read.

Being in the catalogue is not being permitted, in scope, autonomous,
approved, or executable. Six questions, six answers, and A4 collapses
none of them (A21.3).

WHY IT EXISTS
-------------
`REMEDIATION_CANDIDATES` was a module constant. An agent could propose
only what a hardcoded dict named; an operator could neither see it nor
change it. The platform implements 12 of its 14 action types and an
agent could propose 6 -- so seven implemented, governed, node-executable
capabilities were invisible to every agent. Not forbidden. Unreachable.

THE SUBSYSTEM VOCABULARY IS NOT INVENTED HERE
---------------------------------------------
Every key below is a condition the runtime actually produces:
`health_summary` keys from the shipped skills (disk, fan, memory, psu,
thermal, interface) and `sensor_id` prefixes from the agent
(`log:sel`, `config:<policy>`, `os:*`), plus the synthesized `bmc`
condition for an unreachable controller.

Keying an entry on a condition that never occurs is exactly the failure
this slice exists to fix -- the `interface` subsystem mapped only to
`CLEAR_COUNTERS`, which no executor implements, so a switch-scoped agent
could observe an interface incident and never act on it.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

# ---------------------------------------------------------------------------
# The seed (A21.1)
# ---------------------------------------------------------------------------
#
# Seeded so that NO tenant's behaviour changes on upgrade, except the one
# correction A21.4 mandates: `interface` stops naming an unimplemented
# class and names the gNMI actions R6 actually shipped.
#
# `provenance` is kept per entry, exactly as the constant carried it: an
# operator asking "why can my agent propose this?" gets an answer that
# names where the mapping came from.

#: The synthesized condition for a management controller that stopped
#: answering while its node kept reporting. Not a reported subsystem --
#: the evaluator derives it, which is why it is named separately.
SUBSYSTEM_UNREACHABLE = "bmc"

SEED: tuple[dict[str, str], ...] = (
    # -- unchanged from REMEDIATION_CANDIDATES ------------------------------
    {"subsystem": "disk", "action_type": "IDENTIFY_LED",
     "because": "a failing drive has to be found before it can be replaced",
     "provenance": "skills/disk-health.yaml"},
    {"subsystem": "fan", "action_type": "COLLECT_DIAGNOSTICS",
     "because": "capture the fault state before the evidence rotates away",
     "provenance": "skills/fan-health.yaml"},
    {"subsystem": "memory", "action_type": "COLLECT_DIAGNOSTICS",
     "because": "capture the fault state before the evidence rotates away",
     "provenance": "skills/memory-health.yaml"},
    {"subsystem": "psu", "action_type": "COLLECT_DIAGNOSTICS",
     "because": "capture the fault state before the evidence rotates away",
     "provenance": "skills/psu-health.yaml"},
    {"subsystem": "thermal", "action_type": "FAN_RESET",
     "because": "a stuck fan controller is the recoverable half of a thermal fault",
     "provenance": "skills/thermal-health.yaml"},
    {"subsystem": "log", "action_type": "SEL_CLEAR",
     "because": "a saturated event log hides the next fault behind the last one",
     "provenance": "A2.1 action semantics; granted at autonomy level 2"},
    {"subsystem": SUBSYSTEM_UNREACHABLE, "action_type": "BMC_RESET",
     "because": "the management controller stopped answering while the node kept reporting",
     "provenance": "R3a action semantics; granted at autonomy level 2"},

    # -- A21.4: the interface subsystem, repaired ---------------------------
    # It mapped ONLY to CLEAR_COUNTERS, which no executor implements, so
    # A17's zero-reach rule refused the binding and a switch-scoped agent
    # had no proposable action at all. These two shipped in R6.
    {"subsystem": "interface", "action_type": "INTERFACE_DISABLE",
     "because": ("isolate a failing interface so its fault stops propagating; "
                 "the node's self-preservation resolver refuses if this would "
                 "cut the path it is answering on"),
     "provenance": "R6 P6 action safety chain (A9)"},
    {"subsystem": "interface", "action_type": "INTERFACE_ENABLE",
     "because": "bring an interface back once the fault it carried has cleared",
     "provenance": "R6 P6 action safety chain (A9)"},

    # -- A21: implemented classes that had no path to an agent --------------
    {"subsystem": "thermal", "action_type": "POWER_CAP_ADJUST",
     "because": ("lowering the power cap lowers the heat a node produces, "
                 "which is the other half of a thermal fault the fan cannot fix"),
     "provenance": "R3a action semantics"},
    {"subsystem": SUBSYSTEM_UNREACHABLE, "action_type": "POWER_CYCLE",
     "because": ("a controller that does not come back from a reset is the case "
                 "power cycling exists for"),
     "provenance": "R3a action semantics"},
    {"subsystem": "config", "action_type": "CONFIG_RESTORE",
     "because": ("configuration has drifted from the policy this device was "
                 "given; restoring it is the remediation that drift detection "
                 "was built to trigger"),
     "provenance": "R4-2 config drift detection + CONFIG_RESTORE"},
    {"subsystem": "os", "action_type": "COLLECT_DIAGNOSTICS",
     "because": "capture the fault state before the evidence rotates away",
     "provenance": "R3a OS signals (syslog/dmesg); R3b-1 journal/smartctl"},
)

#: Deliberately ABSENT from the seed, and from any condition mapping.
#:
#: FIRMWARE_UPDATE and FIRMWARE_ROLLBACK are implemented and reachable --
#: through S6 campaigns, which is the governed path they have. An agent
#: does not propose a firmware update in response to a fault, and
#: inventing a condition for them would be inventing a remediation model
#: nobody asked for (A21.10).
CAMPAIGN_ONLY_CLASSES = frozenset({"FIRMWARE_UPDATE", "FIRMWARE_ROLLBACK"})


# ---------------------------------------------------------------------------
# Validation (A21.2, A21.9)
# ---------------------------------------------------------------------------

REFUSE_UNKNOWN_CLASS = "is not an action class this platform governs"
REFUSE_UNIMPLEMENTED = (
    "is in the governed vocabulary but no executor on this platform "
    "implements it, so a condition can never be remediated by it"
)
REFUSE_NO_SUBSYSTEM = "a catalogue entry needs a subsystem"
REFUSE_CAMPAIGN_ONLY = (
    "is reachable through campaigns, which is the governed path it has; "
    "an agent does not propose it in response to a fault"
)


def validate_entry(
    subsystem: str, action_type: str, *, known: Iterable[str],
    implemented: Iterable[str],
) -> tuple[bool, str]:
    """May this condition -> capability mapping exist? (A21.2.)

    Refuses on CAPABILITY, never on policy -- the A17.7 boundary. A class
    whose node allow lists happen to exclude it today is still a valid
    catalogue entry: an allow list is mutable operator policy, and
    refusing on it would make it impossible to configure ahead of a
    config rollout. What cannot be mapped is a class no executor
    IMPLEMENTS, because no amount of policy will make that executable.
    """
    if not (subsystem or "").strip():
        return False, REFUSE_NO_SUBSYSTEM
    name = (action_type or "").upper()
    if name not in set(known):
        return False, f"{action_type!r} {REFUSE_UNKNOWN_CLASS}"
    if name in CAMPAIGN_ONLY_CLASSES:
        return False, f"{name} {REFUSE_CAMPAIGN_ONLY}"
    if name not in set(implemented):
        return False, f"{name} {REFUSE_UNIMPLEMENTED}"
    return True, ""


def candidates_for(rows: Iterable[Any], subsystem: str) -> list[dict[str, str]]:
    """The candidate actions for one observed condition.

    Returns the same shape `REMEDIATION_CANDIDATES` returned, so the
    evaluator's call site is unchanged: a mapping that moved from a
    constant into a table must not also change what the caller sees.

    A subsystem absent from the catalogue yields NO candidate. Silence is
    the correct answer -- inventing a remediation for a condition nobody
    mapped is exactly what the catalogue exists to prevent.
    """
    key = (subsystem or "").lower()
    return [
        {"action_type": r.action_type, "because": r.because,
         "provenance": r.provenance}
        for r in rows
        if r.enabled and (r.subsystem or "").lower() == key
    ]


def catalogue_view(rows: Iterable[Any], registry: Optional[dict] = None) -> dict:
    """What an operator reads. Registry reach joined, never merged.

    A17.7's rule, applied to a third consumer: capability is reported
    BESIDE the mapping, never folded into it. "This condition maps to
    this action" and "an executor can currently perform it here" are
    different facts, and an operator debugging a silent agent needs to
    see which one is missing.
    """
    reach_by_class: dict[str, dict] = {}
    for row in (registry or {}).get("classes", []) or []:
        reach_by_class[row.get("action_type", "")] = {
            "implemented": row.get("capability", {}).get("implemented")
            if isinstance(row.get("capability"), dict) else row.get("implemented"),
            "reach": row.get("reach"),
            "reachable_devices": row.get("reachable_devices"),
        }

    by_subsystem: dict[str, list] = {}
    for row in rows:
        by_subsystem.setdefault(row.subsystem, []).append({
            "action_type": row.action_type,
            "because": row.because,
            "provenance": row.provenance,
            "enabled": bool(row.enabled),
            # Beside, not merged.
            "capability": reach_by_class.get(row.action_type),
        })
    for entries in by_subsystem.values():
        entries.sort(key=lambda e: e["action_type"])

    return {
        "subsystems": [
            {"subsystem": name, "candidates": by_subsystem[name]}
            for name in sorted(by_subsystem)
        ],
        "contract": {
            "authority": (
                "The catalogue says which capability is a CANDIDATE for an "
                "observed condition. It grants nothing: every proposal still "
                "passes RBAC, scope, the Capability Registry, the autonomy "
                "contract, the approval ledger and the node's own funnel."
            ),
            "registry": (
                "The Capability Registry remains the only authority on whether "
                "an executor can perform an action. A catalogue entry can never "
                "make an unimplemented class executable."
            ),
            "autonomy": (
                "Being addressable is not being autonomous. A class that is not "
                "budget-mapped requires a named human, however effective it has "
                "proven to be."
            ),
        },
    }
