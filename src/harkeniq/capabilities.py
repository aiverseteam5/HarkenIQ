"""Capability declaration: what an executor can ACTUALLY do.

The Capability Registry has exactly one authoritative source, and it is
here: the node's own declaration of its implementation reach, intersected
with the allow list that node is configured to permit. Everything above
this file -- the Site Manager column, the Central Command cache, the
``/api/capabilities`` contract, the Operational Agent's binding and
proposal validation, the Console page -- REFLECTS this. None of them may
declare a capability of their own.

The distinction that makes the Registry worth having::

    capability   what the executor can do      <- this module
    permission   who may ask for it            <- RBAC (E1.2)
    scope        where they may ask for it     <- scope grants (E1.2)
    autonomy     may it run unattended         <- the S5 contract
    approval     must a human decide           <- approval policy (E0.1)
    execution    may it happen right now       <- the node's allow list

They are six different questions. This module answers only the first, and
answering it truthfully is the entire job: an action class can be fully
governed -- risk classified, preconditions written, blast radius modelled,
verification specified -- and still have no code behind it on any protocol
this platform ships. INTERFACE_RESET is exactly that today, and the
Registry's purpose is to say so out loud rather than let a governed
vocabulary be mistaken for a working one.

Nothing here gates execution. The node's allow list remains the final
execution authority; connecting Registry truth to the runtime
authorization path is the named follow-up (the capability execution gate)
and is deliberately not attempted here.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from harkeniq.autonomy.preconditions import (
    ACTION_REVERSIBILITY,
    ACTION_RISK,
    INVERSE_ACTION,
)
from harkeniq.models import ActionType

#: Bumped when the declaration payload's shape changes, so a Site Manager
#: or Central Command reading an older node's declaration can tell the
#: difference between "this node speaks an older dialect" and "this node
#: declared nothing".
DECLARATION_VERSION = 1

#: The protocols this build ships, by the name ``create_device_protocol``
#: accepts. Kept next to the factory's own list; a protocol added there
#: and forgotten here is caught by test_capability_declaration_truthful.
PROTOCOL_NAMES = ("redfish", "ipmi", "gnmi")


def _protocol_class(name: str):
    if name == "redfish":
        from harkeniq.protocols.redfish import RedfishDeviceProtocol

        return RedfishDeviceProtocol
    if name == "ipmi":
        from harkeniq.protocols.ipmi import IPMIProtocol

        return IPMIProtocol
    if name == "gnmi":
        from harkeniq.protocols.gnmi import GNMIProtocol

        return GNMIProtocol
    return None


def protocol_reach(protocol_name: str) -> Optional[frozenset[str]]:
    """What ``protocol_name``'s code implements, or None if unknown.

    None is a real answer and must stay distinguishable from the empty
    set: an unrecognised protocol has UNKNOWN reach, which is not the
    same claim as "implements nothing".
    """
    cls = _protocol_class((protocol_name or "").lower())
    if cls is None:
        return None
    declared = getattr(cls, "supported_actions", None)
    if declared is None:
        return None
    return frozenset(str(a) for a in declared())


def protocol_reach_of(protocol: Any) -> Optional[frozenset[str]]:
    """Reach of a live protocol INSTANCE, or None if it does not declare."""
    declared = getattr(protocol, "supported_actions", None)
    if declared is None:
        return None
    try:
        return frozenset(str(a) for a in declared())
    except Exception:  # pragma: no cover - a broken declaration is unknown
        return None


def platform_implementations() -> dict[str, list[str]]:
    """action class -> the protocols implementing it, across this build.

    Every ActionType appears. A class implemented by nothing maps to an
    empty list, which is the Registry's ``implemented: false``. That row
    is the point of the whole exercise -- it is not an error and must not
    be filtered away to make the output look tidy.
    """
    out: dict[str, list[str]] = {a.value: [] for a in ActionType}
    for name in PROTOCOL_NAMES:
        reach = protocol_reach(name)
        if not reach:
            continue
        for action in reach:
            if action in out:
                out[action].append(name)
    return {k: sorted(v) for k, v in out.items()}


def action_facts() -> dict[str, dict[str, Any]]:
    """The platform-level truth about every governed action class.

    Risk and reversibility are read from their single definitions; the
    implementing protocols are read from the protocols themselves. This
    function restates nothing.
    """
    impls = platform_implementations()
    facts: dict[str, dict[str, Any]] = {}
    for action in ActionType:
        inverse = INVERSE_ACTION.get(action)
        facts[action.value] = {
            "action_type": action.value,
            "risk": ACTION_RISK[action],
            "reversibility": ACTION_REVERSIBILITY[action],
            "inverse_action": inverse.value if inverse else None,
            "implemented_by": impls[action.value],
            "implemented": bool(impls[action.value]),
        }
    return facts


def declare(
    protocol_name: str,
    allow_list: Iterable[str],
    device_class: str = "server",
) -> dict[str, Any]:
    """Build this node's capability declaration.

    ``implemented`` is what the code can dispatch; ``allow_list`` is what
    this node permits; ``effective`` is the intersection -- the only set
    that answers "can this device actually perform this action". Sending
    all three rather than just the intersection is deliberate: an
    operator looking at a device that will not act needs to know whether
    the answer is "no code" or "not permitted here", and those call for
    completely different responses.
    """
    reach = protocol_reach(protocol_name)
    permitted = sorted({str(a) for a in (allow_list or [])})
    declaration: dict[str, Any] = {
        "version": DECLARATION_VERSION,
        "protocol": (protocol_name or "").lower(),
        "device_class": device_class or "server",
        "allow_list": permitted,
    }
    if reach is None:
        # Truthful unknown. Consumers must not read this as either
        # capable or incapable, and must not compute an intersection
        # from a set they do not have.
        declaration["implemented"] = None
        declaration["effective"] = None
        declaration["reach_known"] = False
        return declaration
    declaration["implemented"] = sorted(reach)
    declaration["effective"] = sorted(reach & set(permitted))
    declaration["reach_known"] = True
    return declaration


def implemented_actions(declaration: Optional[dict]) -> Optional[frozenset[str]]:
    """What a device's PROTOCOL implements, or None when unknown.

    The capability half of a declaration, without the node's allow list
    applied. This is what a consumer DECIDES with; `effective_actions` is
    what it displays.

    Lives here rather than in one service because three of them now ask
    the question -- Central Command's registry, its agent preflight, and
    the Site Manager's execution gate (A21.6). A second copy would be a
    second answer to "can this device do this", which is the one thing
    the Registry exists to prevent.
    """
    if not isinstance(declaration, dict):
        return None
    if not declaration.get("reach_known"):
        return None
    implemented = declaration.get("implemented")
    if implemented is None:
        return None
    return frozenset(str(a) for a in implemented)


def effective_actions(declaration: Optional[dict]) -> Optional[frozenset[str]]:
    """What a declared device can actually do, or None when unknown.

    None means the device has not declared (a node predating the
    Registry, or one whose protocol does not declare its reach). Callers
    MUST keep None distinct from the empty set: empty is a proven "no",
    None is "we do not know", and treating the second as the first would
    silently strip capability from every fleet that has not upgraded yet.
    """
    if not isinstance(declaration, dict):
        return None
    if not declaration.get("reach_known"):
        return None
    effective = declaration.get("effective")
    if effective is None:
        return None
    return frozenset(str(a) for a in effective)
