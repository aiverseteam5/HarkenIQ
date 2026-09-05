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

import json
from typing import Any, Iterable, Optional

from harkeniq.autonomy.preconditions import (
    ACTION_PARAMETERS,
    ACTION_REVERSIBILITY,
    ACTION_RISK,
    INVERSE_ACTION,
    PTYPE_INTEGER,
    PTYPE_JSON_OBJECT,
    PTYPE_STRING,
    REASON_PARAM,
    SRC_CAMPAIGN,
    SRC_COMPONENT,
    SRC_UNAVAILABLE,
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
            # A22.2: what the class REQUIRES, beside what implements it.
            **parameter_contract(action.value),
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


# ---------------------------------------------------------------------------
# Action parameter contract: validation and resolution (spec A22.2, A22.3)
# ---------------------------------------------------------------------------
#
# The declaration lives beside ACTION_RISK; the behaviour lives here, next
# to `action_facts`, because every consumer that already asks this module
# "what can this executor do" now asks it "and what does that require".
# One import site, one answer.

def parameter_specs(action_type: str) -> tuple:
    """The declared parameters of one action class. () for unknown."""
    try:
        return ACTION_PARAMETERS[ActionType(action_type)]
    except (KeyError, ValueError):
        return ()


def parameter_contract(action_type: str) -> dict:
    """The parameter contract as a consumer reads it.

    `satisfiable` is the fact A22.5 exists for: a class can be governed,
    implemented and permitted and still have no way to obtain a truthful
    value for something it requires. That is reported by name, never
    hidden and never presented as executable.
    """
    specs = parameter_specs(action_type)
    unsatisfiable = [
        s for s in specs if s.required and s.source == SRC_UNAVAILABLE
    ]
    return {
        "parameters": [
            {
                "name": s.name,
                "type": s.type,
                "required": s.required,
                "source": s.source,
                "default": s.default,
                "constraint": s.constraint,
                "missing_input": s.missing_input,
            }
            for s in specs
        ] + [{
            "name": REASON_PARAM.name,
            "type": REASON_PARAM.type,
            "required": False,
            "source": REASON_PARAM.source,
            "default": None,
            "constraint": REASON_PARAM.constraint,
            "missing_input": "",
        }],
        "required": [s.name for s in specs if s.required],
        "agent_resolvable": not unsatisfiable,
        "unsatisfiable_reason": (
            f"{unsatisfiable[0].name}: {unsatisfiable[0].missing_input}"
            if unsatisfiable else ""
        ),
    }


def _type_ok(spec, value) -> tuple[bool, str]:
    if spec.type == PTYPE_STRING:
        if not isinstance(value, str) or not value.strip():
            return False, f"{spec.name!r} must be a non-empty string"
        return True, ""
    if spec.type == PTYPE_INTEGER:
        if isinstance(value, bool):
            return False, f"{spec.name!r} must be an integer"
        if isinstance(value, int):
            return True, ""
        try:
            int(str(value))
        except (TypeError, ValueError):
            return False, f"{spec.name!r} must be an integer"
        return True, ""
    if spec.type == PTYPE_JSON_OBJECT:
        # The wire carries a JSON STRING; the executor parses it. Matching
        # the executor exactly is the point -- a contract that accepts what
        # the executor rejects is not a contract.
        if not isinstance(value, str) or not value.strip():
            return False, f"{spec.name!r} must be a JSON object as a string"
        try:
            parsed = json.loads(value)
        except ValueError as e:
            return False, f"{spec.name!r} is not valid JSON: {e}"
        if not isinstance(parsed, dict) or not parsed:
            return False, f"{spec.name!r} must be a non-empty JSON object"
        return True, ""
    return False, f"{spec.name!r} has an undeclared type {spec.type!r}"


def validate_action_params(action_type: str, params: dict) -> tuple[bool, str]:
    """Does this payload satisfy the class's declared contract? (A22.3.)

    Called BEFORE a proposal exists, and by skill validation. Strict on
    unknown keys deliberately: a typo caught here is caught once, while a
    typo that reaches the node is a proposal a human approves, a dispatch
    that travels three services, and a refusal whose cause is invisible.
    """
    try:
        action = ActionType(action_type)
    except ValueError:
        return False, f"{action_type!r} is not an action class this platform governs"

    specs = {s.name: s for s in ACTION_PARAMETERS[action]}
    specs[REASON_PARAM.name] = REASON_PARAM
    given = dict(params or {})

    for name in sorted(given):
        if name not in specs:
            return False, (
                f"{action.value} does not declare a parameter {name!r} "
                f"(declared: {', '.join(sorted(specs)) or 'none'})"
            )
    for name, spec in sorted(specs.items()):
        if name not in given:
            if spec.required:
                return False, f"{action.value} requires a {name!r} parameter"
            continue
        ok, why = _type_ok(spec, given[name])
        if not ok:
            return False, why
    return True, ""


def operation_identity(action_type: str, params: Optional[dict] = None) -> str:
    """The smallest stable description of ONE mutually exclusive operation.

    A24.12. Attribution answers *who proposed this*; this answers *is this
    the same physical thing*. They must not be the same key: the proposal
    dedupe key begins with the proposing agent, so two agents could hold
    two simultaneously active proposals to do one thing to one device.

    DERIVED FROM THE CONTRACT, never hard-coded. `ACTION_PARAMETERS`
    already says which parameters address the affected component --
    `SRC_COMPONENT`, whose section is titled "the affected component IS
    the parameter" -- and which carry no executor meaning at all
    (`SRC_ANNOTATION`: "no executor reads it"). Reading that declaration
    means this identity follows the contract if the contract changes,
    instead of becoming a second, quietly diverging opinion about what a
    target is.

    A class with no component parameter identifies the device as a whole,
    which is correct: two SEL_CLEARs on one device ARE one operation.
    Two DIFFERENT classes, or one class against two components, stay
    legitimately concurrent.

    Returns a canonical string; callers scope it with tenant and device.
    """
    try:
        action = ActionType(action_type)
    except ValueError:
        # Unknown to the platform: identify it by name alone rather than
        # raise. A caller asking about a class we do not govern gets a
        # stable answer, and governance refuses it elsewhere.
        return f"{action_type}"
    values = params or {}
    addressing = [
        spec.name for spec in ACTION_PARAMETERS[action]
        if spec.source == SRC_COMPONENT
    ]
    parts = [action.value]
    for name in sorted(addressing):
        parts.append(f"{name}={values.get(name, '')}")
    return "|".join(parts)


def operation_key(
    tenant_id: str, device_agent_id: str, action_type: str,
    params: Optional[dict] = None,
) -> str:
    """`operation_identity` scoped to the device it acts on.

    Digested so it fits an indexed column and carries no readable
    structure; equality is the only property anything needs from it.
    """
    import hashlib

    payload = "|".join((
        "op.v1", tenant_id, device_agent_id,
        operation_identity(action_type, params),
    ))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:48]


def resolve_action_params(
    action_type: str, *, component: str = "", reason: str = "",
) -> tuple[Optional[dict], str]:
    """Build a valid payload from reported evidence, or refuse and say why.

    This is the whole of A22.4's consumer side. It NEVER guesses: a class
    needing a component gets one only if the Site Manager reported one,
    and a class needing something this platform cannot supply is refused
    with the missing input named. Returning ``({"reason": ...}, "")`` for
    everything is precisely the defect A5 exists to fix.
    """
    try:
        action = ActionType(action_type)
    except ValueError:
        return None, f"{action_type!r} is not an action class this platform governs"

    params: dict[str, Any] = {}
    if reason:
        params[REASON_PARAM.name] = reason

    for spec in ACTION_PARAMETERS[action]:
        if spec.source == SRC_UNAVAILABLE:
            if spec.required:
                return None, (
                    f"{action.value} requires {spec.name!r} and "
                    f"{spec.missing_input}"
                )
            continue
        if spec.source == SRC_COMPONENT:
            if not component:
                return None, (
                    f"{action.value} requires {spec.name!r}, which names the "
                    f"affected component, and no component was reported for "
                    f"this condition"
                )
            params[spec.name] = component
            continue
        if spec.source == SRC_CAMPAIGN:
            if spec.required:
                return None, (
                    f"{action.value} requires {spec.name!r}, which only "
                    f"campaign orchestration supplies; it is not proposed "
                    f"in response to a fault"
                )
            continue
        # SRC_DEFAULT: the executor applies the same value. Leaving it out
        # keeps the payload honest about what was actually decided.

    ok, why = validate_action_params(action.value, params)
    if not ok:  # pragma: no cover - the loop above cannot produce this
        return None, why
    return params, ""
