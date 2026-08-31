"""The Capability Registry composer: what the fleet can ACTUALLY do.

Pure. Every input is fetched by the router and handed in, so the whole
contract is unit-testable without a database -- the same shape as
`autonomy.build_autonomy`, deliberately, because these two reads answer
adjacent questions and an operator will hold them side by side.

    /api/autonomy      MAY this class run without a human?
    /api/capabilities  CAN this class run at all, and on which devices?

The second question was never asked anywhere in the platform before this
slice, and its absence was load-bearing. A class could be fully governed
-- risk classified, preconditions written, blast radius modelled,
approval policy attached, autonomy grantable -- while no protocol this
platform ships had a single line of code behind it. The Operational
Agent would then propose it, a human would approve it, a directive would
be dispatched, and the node would refuse it. Every time. Silently, from
the operator's point of view, because nothing upstream had any way to
know. INTERFACE_RESET is that, and so is CLEAR_COUNTERS.

WHAT THIS COMPOSER MAY NOT DO
-----------------------------
It reflects. It has no capability model of its own and must never grow
one: the node declares, the Site Manager stores, this composes, the
Console and the Operational Agent read. Risk comes from ACTION_RISK,
reversibility from ACTION_REVERSIBILITY, implementation from the
protocols themselves, per-device reach from each node's declaration.
Nothing here is authored; if a fact needs inventing, it belongs at its
source, not in this file.

It also confers nothing. Capability is not permission, not scope, not
autonomy, not approval, and not execution authority -- six different
questions with six different answers, and the node's allow list remains
the final one. Connecting this truth into the runtime authorization path
is the named capability-execution-gate follow-up and is deliberately not
attempted here.

UNKNOWN IS A REAL ANSWER
------------------------
A device that has not declared reads `unknown`, never "capable" and
never "incapable". A fleet that upgrades Central Command before its
agents is entirely unknown for a while, and reporting that honestly is
what lets the Operational Agent keep working through the upgrade instead
of losing every binding to a refusal it cannot explain.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from harkeniq.capabilities import action_facts, effective_actions

#: A class no protocol in this build implements. It keeps every governed
#: semantic it has -- risk, preconditions, blast radius, verification --
#: and simply cannot be executed by anything the platform ships.
REACH_UNIMPLEMENTED = "unimplemented"
#: Implemented somewhere, but no device in view declares it effective.
REACH_NONE = "no_effective_reach"
#: Implemented, and at least one device in view can actually do it.
REACH_AVAILABLE = "available"
#: Implemented, and every device in view has yet to declare. Not a "no".
REACH_UNKNOWN = "unknown"

#: Why a class cannot reach a device, in the order the reasons are
#: checked. Each is actionable and names a different fix.
WHY_UNIMPLEMENTED = "no_executor_implements_it"
WHY_PROTOCOL = "device_protocol_does_not_implement_it"
WHY_ALLOW_LIST = "not_on_this_node_allow_list"
WHY_UNDECLARED = "device_has_not_declared"


def _device_row(device) -> dict[str, Any]:
    declaration = getattr(device, "capabilities", None)
    if not isinstance(declaration, dict):
        declaration = None
    effective = effective_actions(declaration)
    return {
        "agent_id": getattr(device, "agent_id", ""),
        "agent_name": getattr(device, "agent_name", ""),
        "site_id": getattr(device, "site_id", ""),
        "vendor": getattr(device, "vendor", ""),
        "model": getattr(device, "model", ""),
        "device_class": getattr(device, "device_class", "") or "server",
        "declared": declaration is not None,
        "protocol": (declaration or {}).get("protocol") or None,
        "declaration_version": (declaration or {}).get("version"),
        "implemented": sorted((declaration or {}).get("implemented") or [])
        if declaration and declaration.get("reach_known") else None,
        "allow_list": sorted((declaration or {}).get("allow_list") or [])
        if declaration else None,
        "effective": sorted(effective) if effective is not None else None,
        "_effective_set": effective,
    }


def device_capability_reason(row: dict, action_type: str, fact: dict) -> str:
    """Why this device cannot run this class, or "" when it can.

    Ordered from most to least fundamental, so the reason an operator
    reads is the one that actually has to change first: no code anywhere
    beats no code on this protocol beats not permitted on this node.
    """
    if not fact["implemented"]:
        return WHY_UNIMPLEMENTED
    if not row["declared"] or row["_effective_set"] is None:
        return WHY_UNDECLARED
    if action_type in row["_effective_set"]:
        return ""
    implemented = set(row["implemented"] or ())
    if action_type not in implemented:
        return WHY_PROTOCOL
    return WHY_ALLOW_LIST


def build_capability_registry(
    *,
    tenant_id: str,
    devices: Iterable[Any],
    sites: Iterable[Any] = (),
    action_type: Optional[str] = None,
    site_id: Optional[str] = None,
    max_devices_listed: int = 25,
) -> dict[str, Any]:
    """Compose the Registry over the devices the caller may see.

    `devices` is already tenant-scoped AND E1.2 scope-filtered by the
    repository; this function narrows no further and widens nothing. A
    caller who can see no devices gets every class with unknown or zero
    reach, which is the correct answer for a principal with no fleet --
    not an error, and not the whole fleet.
    """
    rows = [_device_row(d) for d in devices]
    if site_id:
        rows = [r for r in rows if r["site_id"] == site_id]
    site_names = {
        getattr(s, "id", ""): getattr(s, "name", "") for s in sites
    }

    facts = action_facts()
    wanted = sorted(facts)
    if action_type:
        wanted = [a for a in wanted if a == action_type.upper()]

    classes: list[dict[str, Any]] = []
    for name in wanted:
        fact = facts[name]
        capable: list[dict] = []
        undeclared = 0
        blocked: dict[str, int] = {}
        capable_sites: set[str] = set()
        for row in rows:
            reason = device_capability_reason(row, name, fact)
            if reason == "":
                capable.append(row)
                if row["site_id"]:
                    capable_sites.add(row["site_id"])
                continue
            if reason == WHY_UNDECLARED:
                undeclared += 1
            blocked[reason] = blocked.get(reason, 0) + 1

        if not fact["implemented"]:
            reach = REACH_UNIMPLEMENTED
        elif capable:
            reach = REACH_AVAILABLE
        elif rows and undeclared == len(rows):
            reach = REACH_UNKNOWN
        elif not rows:
            reach = REACH_UNKNOWN
        else:
            reach = REACH_NONE

        classes.append({
            "action_type": name,
            "risk": fact["risk"],
            "reversibility": fact["reversibility"],
            "inverse_action": fact["inverse_action"],
            # -- what the PLATFORM implements ---------------------------
            "implemented": fact["implemented"],
            "implemented_by": fact["implemented_by"],
            # -- what THIS caller's fleet can actually do ---------------
            "reach": reach,
            "effective_device_count": len(capable),
            "undeclared_device_count": undeclared,
            "devices_in_view": len(rows),
            "effective_sites": [
                {"id": s, "name": site_names.get(s, "")}
                for s in sorted(capable_sites)
            ],
            "effective_devices": [
                {
                    "agent_id": r["agent_id"],
                    "agent_name": r["agent_name"],
                    "site_id": r["site_id"],
                    "device_class": r["device_class"],
                    "protocol": r["protocol"],
                }
                for r in capable[:max_devices_listed]
            ],
            "effective_devices_truncated": len(capable) > max_devices_listed,
            "blocked_by": [
                {"reason": reason, "device_count": count}
                for reason, count in sorted(blocked.items())
            ],
            "reason": _class_reason(name, fact, reach, len(capable), undeclared,
                                    len(rows)),
        })

    declared = sum(1 for r in rows if r["declared"])
    return {
        "tenant_id": tenant_id,
        "generated_for": {"site_id": site_id, "action_type": action_type},
        "fleet": {
            "devices_in_view": len(rows),
            "declared": declared,
            "undeclared": len(rows) - declared,
            "protocols": sorted(
                {r["protocol"] for r in rows if r["protocol"]}
            ),
        },
        "classes": classes,
        "contract": {
            "authority": (
                "This Registry describes what an executor can do. It is "
                "not permission, scope, autonomy, approval or execution "
                "authority, and it grants none of them. The node's own "
                "allow list remains the final execution authority."
            ),
            # The two words the whole contract turns on. A consumer that
            # confuses them refuses work the platform can perform.
            "implemented": (
                "CAPABILITY EXISTENCE: a real handler exists in this "
                "build for this action class, on the named protocols. "
                "This is the Registry's actual answer, it is immutable "
                "for a given build and device, and it is the ONLY ground "
                "on which a binding or a proposal may be refused."
            ),
            "effective": (
                "A CONFIGURATION/READINESS PROJECTION, not a definition "
                "of capability existence: implemented AND currently "
                "permitted by the node's own allow list. The allow list "
                "is operator configuration that can change at any time, "
                "so 'effective' describes readiness right now and "
                "nothing more. Every 'effective_*' field on a class row, "
                "and 'effective' on a device declaration, carries this "
                "meaning. An action that is implemented but not "
                "currently permitted MUST still bind and still be "
                "proposed; the node evaluates its allow list at "
                "execution time and its refusal becomes attributed "
                "evidence."
            ),
            "refusal": (
                "A binding or proposal is refused only for absence of "
                "implementation -- no executor in the platform has it, "
                "or no protocol among the devices in scope has it. It is "
                "never refused because a node does not currently permit "
                "it."
            ),
            "unknown": (
                "A device that has not declared reads 'unknown', never "
                "capable and never incapable. Unknown reach does not "
                "block a proposal; provable zero reach does."
            ),
        },
    }


def _class_reason(
    action_type: str,
    fact: dict,
    reach: str,
    capable: int,
    undeclared: int,
    in_view: int,
) -> str:
    if reach == REACH_UNIMPLEMENTED:
        return (
            f"{action_type} is a governed action class with no "
            f"implementation on any protocol this platform ships. Its "
            f"risk level, preconditions and blast-radius semantics are "
            f"intact; there is simply no executor behind it, so no "
            f"device can run it and no agent may be bound to it."
        )
    if reach == REACH_AVAILABLE:
        return (
            f"{capable} of {in_view} device(s) in view can execute "
            f"{action_type} today (implemented by "
            f"{', '.join(fact['implemented_by'])})."
        )
    if reach == REACH_UNKNOWN:
        if in_view == 0:
            return (
                f"No devices in view, so this caller has no effective "
                f"reach for {action_type} to report."
            )
        return (
            f"{action_type} is implemented by "
            f"{', '.join(fact['implemented_by'])}, but none of the "
            f"{in_view} device(s) in view has declared its capabilities "
            f"yet. Reach is unknown, not zero."
        )
    return (
        f"{action_type} is implemented by "
        f"{', '.join(fact['implemented_by'])}, but no device in view can "
        f"execute it: the protocol in use does not implement it, or the "
        f"node's allow list does not permit it."
        + (f" {undeclared} device(s) have not declared." if undeclared else "")
    )


def reachable_action_classes(devices: Iterable[Any]) -> dict[str, Any]:
    """What these devices can run, split into CAPABILITY and POLICY.

    Returns ``{"implemented": set, "effective": set, "unknown": bool,
    "devices": int}``:

      implemented  the union of what these devices' PROTOCOLS implement.
                   A capability fact: immutable for a given build and
                   device, and the only ground a consumer may REFUSE on.
      effective    implemented, narrowed by each node's own allow list.
                   A policy fact: an operator can change it this
                   afternoon, and the node enforces it as the final
                   execution authority. Reported, never used to refuse.

    The split is the whole discipline of this module, and getting it
    wrong is easy: an early version of the binding check refused on
    `effective`, which quietly promoted a mutable node policy into a hard
    Central Command configuration constraint and made it impossible to
    configure an agent ahead of a config rollout. The Registry answers
    "can the code do this"; whether a node permits it today is question
    six, and question six belongs to the node.

    `unknown` says whether any device has yet to declare. Consumers must
    use it: blocking on an empty set alone would refuse every binding on
    a fleet that has not upgraded. A refusal is only correct when reach
    is provably zero -- unimplemented platform-wide, or every device
    declared and none of them implementing it.
    """
    implemented: set[str] = set()
    effective: set[str] = set()
    unknown = False
    seen = 0
    for device in devices:
        seen += 1
        declaration = getattr(device, "capabilities", None)
        row_effective = effective_actions(declaration)
        if row_effective is None:
            unknown = True
            continue
        effective |= set(row_effective)
        implemented |= {
            str(a) for a in ((declaration or {}).get("implemented") or [])
        }
    return {
        "implemented": implemented,
        "effective": effective,
        "unknown": unknown,
        "devices": seen,
    }


def implemented_actions(declaration: Optional[dict]) -> Optional[frozenset[str]]:
    """What a device's PROTOCOL implements, or None when unknown.

    The capability half of a declaration, without the node's allow list
    applied. This is what a consumer decides with; ``effective_actions``
    is what it displays.
    """
    if not isinstance(declaration, dict):
        return None
    if not declaration.get("reach_known"):
        return None
    implemented = declaration.get("implemented")
    if implemented is None:
        return None
    return frozenset(str(a) for a in implemented)
