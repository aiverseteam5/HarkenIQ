"""The machine incident contract (spec A30.34, A6-4B2-1).

`GET /api/incidents/` and `GET /api/incidents/{id}` became readable by an
Operational Agent in A6-4A and kept serving it the Console's payload: the
free-text `title`, the raw `correlation` dict (peer device ids, domain
names), `evidence_cited`, and a `recommended_next.summary` that quoted the
model's `suggested_action` as if HarkenIQ had recommended it. An external
runtime is a language model; every one of those is text it reasons with.

EVERY FUNCTION HERE BUILDS ITS ANSWER BY NAMING THE FIELDS IT MAY PASS
(A25.9). None of them takes the human payload and deletes from it: a
subtractive filter leaks the next field somebody adds upstream, which is
how the human DTO reached machines in the first place. They read ROWS, not
the human dictionary, so nothing the Console grows can arrive here.

WHAT A STRING IS, BY POSITION. Every string leaf in a machine incident
response is one of: a closed code (`kind`, `status`, `subsystem`,
`severity`, `origin`, `trust`, `next_step.code`, ...), an opaque id, a
timestamp, or free text inside an object whose `trust` names its class
(`harkeniq_cc.trust`). A consumer never has to inspect a value to know
which it holds.

GENERATED TEXT LIVES IN ONE PLACE. `diagnosis.generated.{summary,
suggested_action, reasoning_steps}`, after S4 has decided whether this
reader may see it at all (A30.29). Nothing is copied out of it, and nothing
here reads its CONTENT: `next_step` knows only that a diagnosis exists.

Pure: no I/O. The route fetches rows and hands them over.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

from harkeniq.models import ActionType
from harkeniq_cc.receipts import VIEW_MACHINE
from harkeniq_cc.trust import (
    TRUST_OPERATOR_SUPPLIED,
    TRUST_UNTRUSTED_TELEMETRY,
    diagnosis_origin,
    diagnosis_trust,
)

CONTRACT_LIST = "incident_list"
CONTRACT_DETAIL = "incident"
CONTRACT_VERSION = "1"

#: Every closed vocabulary below maps what it does not recognise to this,
#: rather than echoing a value nobody vetted.
OTHER = "other"

#: The Site Manager's incident kinds: `device` from its incident service,
#: the rest from its correlation rules.
KINDS = frozenset({
    "device", "shared_power", "rack_thermal", "batch_component",
    "network_ambiguity", "tor_connectivity",
})
STATUSES = frozenset({"open", "resolved"})
#: The conditions the runtime actually produces (the capability catalogue
#: lists the same set), plus the empty subsystem a network_ambiguity
#: incident carries.
SUBSYSTEMS = frozenset({
    "disk", "fan", "memory", "psu", "thermal", "interface", "log", "config",
    "os", "bmc", "",
})
#: `VerdictSeverity`, lower-cased.
SEVERITIES = frozenset({"unknown", "healthy", "trending", "warning", "critical"})
ACTION_TYPES = frozenset(member.value for member in ActionType)
SIGNAL_SCOPES = frozenset({"site", "cohort"})
MARKER_SCOPES = frozenset({"site", "tenant"})

#: Pending approvals in an incident come from the node action queue.
LANE_NODE = "node"

NEXT_REVIEW_PENDING_APPROVAL = "review_pending_approval"
NEXT_REVIEW_DIAGNOSIS = "review_diagnosis"
NEXT_INVESTIGATE = "investigate"
NEXT_STEP_CODES = frozenset({
    NEXT_REVIEW_PENDING_APPROVAL, NEXT_REVIEW_DIAGNOSIS, NEXT_INVESTIGATE,
})
REF_INCIDENT = "incident"
REF_DEVICE = "device"

# -- bounds -------------------------------------------------------------------

#: At most this many components; `truncated` says when there were more.
COMPONENTS_CAP = 32
COMPONENT_TEXT_MAX = 256
SKILL_NAME_MAX = 128
SITE_NAME_MAX = 255
#: `cc_learned_signals.statement` is 512 wide.
STATEMENT_MAX = 512
#: A generated block over these bounds is malformed, and is withheld whole
#: rather than cut: a truncated instruction is still an instruction.
SUMMARY_MAX = 8192
SUGGESTION_MAX = 2048
STEP_MAX = 4096
STEPS_MAX = 64


# -- scalars ------------------------------------------------------------------


def _code(value: Any, allowed: frozenset) -> str:
    return value if isinstance(value, str) and value in allowed else OTHER


def _text(value: Any, limit: int) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _is_text(value: Any, limit: int) -> bool:
    return isinstance(value, str) and len(value) <= limit


def _unit(value: Any) -> Optional[float]:
    """A confidence in [0, 1], or None. A bool is not a number here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0.0 or number > 1.0:
        return None
    return number


def _iso(value: Any) -> Optional[str]:
    """A timestamp, always with its offset, or None.

    The platform stores UTC. An engine that drops the zone (sqlite) hands
    back a naive value, and a Site Manager string may carry none; either is
    UTC, and the contract says so rather than emitting an ambiguous time.
    A string that is not a timestamp is None -- never echoed.
    """
    if isinstance(value, str) and value:
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def stamp(contract: str, now: datetime) -> dict[str, Any]:
    """The four fields every machine incident response opens with (D1)."""
    return {
        "view": VIEW_MACHINE,
        "contract": contract,
        "contract_version": CONTRACT_VERSION,
        "as_of": now.isoformat(),
    }


# -- the pieces ---------------------------------------------------------------


def machine_components(raw: Any) -> dict[str, Any]:
    """The components an incident names (A22.4), every string enveloped.

    NULL is `reported: false` -- the Site Manager did not say, which is
    unknown and never "nothing affected". A value that is not a list is not
    a report either. `[]` is reported, naming nothing.
    """
    if not isinstance(raw, list):
        return {"reported": False, "truncated": False, "items": []}
    items = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        component = entry.get("component")
        if not isinstance(component, str) or not component:
            continue
        severity = entry.get("severity")
        items.append({
            "severity": _code(
                severity.lower() if isinstance(severity, str) else severity,
                SEVERITIES,
            ),
            "at": _iso(entry.get("at")),
            "reported": {
                "trust": TRUST_UNTRUSTED_TELEMETRY,
                "component": component[:COMPONENT_TEXT_MAX],
                "skill_name": _text(entry.get("skill_name"), SKILL_NAME_MAX),
            },
        })
    return {
        "reported": True,
        "truncated": len(items) > COMPONENTS_CAP,
        "items": items[:COMPONENTS_CAP],
    }


def _withheld_generated(trust: str) -> dict[str, Any]:
    """No text at all: the flag is the answer. Constants only."""
    return {
        "trust": trust, "withheld": True,
        "summary": "", "suggested_action": "", "reasoning_steps": [],
    }


def _machine_generated(block: Mapping, trust: str) -> dict[str, Any]:
    """S4's block, then the machine checks: named fields, typed, bounded."""
    if block.get("withheld") is not False:
        return _withheld_generated(trust)
    summary = block.get("summary")
    suggestion = block.get("suggested_action")
    steps = block.get("reasoning_steps")
    well_formed = (
        _is_text(summary, SUMMARY_MAX)
        and _is_text(suggestion, SUGGESTION_MAX)
        and isinstance(steps, list)
        and len(steps) <= STEPS_MAX
        and all(_is_text(step, STEP_MAX) for step in steps)
    )
    if not well_formed:
        return _withheld_generated(trust)
    return {
        "trust": trust, "withheld": False,
        "summary": summary, "suggested_action": suggestion,
        "reasoning_steps": list(steps),
    }


def _machine_marker(marker: Any) -> Optional[dict[str, Any]]:
    """S4's visible marker, rebuilt field by field."""
    if not isinstance(marker, Mapping) or marker.get("scope") not in MARKER_SCOPES:
        return None
    site_id = marker.get("site_id")
    version = marker.get("projection_version")
    if isinstance(version, bool) or not isinstance(version, int):
        return None
    return {
        "scope": marker["scope"],
        "site_id": site_id if isinstance(site_id, str) else None,
        "projection_version": version,
    }


def machine_diagnosis(explanation: Any, view) -> Optional[dict[str, Any]]:
    """The diagnosis: closed origin, allow-listed trust, S4 first.

    `view` is the reader's `LearningView`. It decides, before anything here
    runs, whether the generated block is this reader's to see (A30.29).
    """
    if not explanation:
        return None
    stored = explanation if isinstance(explanation, Mapping) else {}
    provider = stored.get("provider")
    trust = diagnosis_trust(provider)
    block, marker = view.generated(stored)
    generated = _machine_generated(block, trust)
    return {
        "origin": diagnosis_origin(provider),
        "trust": trust,
        "confidence": _unit(stored.get("confidence")),
        "generated": generated,
        "generation_visibility": (
            None if generated["withheld"] else _machine_marker(marker)
        ),
    }


def machine_prior_learning(signals: Iterable[Mapping]) -> list[dict[str, Any]]:
    """Learned signals, AFTER S4 and R2 have projected them for this reader.

    The statement interpolates the BMC-reported vendor and model, so it is
    telemetry-class text. No evidence dict, no counts, no source ids.
    """
    out = []
    for signal in signals:
        scope_type = signal.get("scope_type")
        site_ref = signal.get("scope_ref")
        out.append({
            "scope_type": _code(scope_type, SIGNAL_SCOPES),
            "site_id": (
                site_ref if scope_type == "site" and isinstance(site_ref, str)
                else None
            ),
            "action_type": _code(signal.get("action_type"), ACTION_TYPES),
            "statement": {
                "trust": TRUST_UNTRUSTED_TELEMETRY,
                "text": _text(signal.get("statement"), STATEMENT_MAX),
            },
            "confidence": _unit(signal.get("confidence")),
            "last_confirmed_at": _iso(signal.get("last_confirmed_at")),
        })
    return out


def machine_approvals(routes: Iterable[Any]) -> dict[str, Any]:
    """Pending node actions on the incident's device, as a conclusion.

    That something is waiting, of which class, since when. Never the
    action's handle, who asked, who may decide, or under which policy.
    """
    pending = [
        {
            "action_type": _code(route.action_type, ACTION_TYPES),
            "lane": LANE_NODE,
            "awaiting_since": _iso(route.routed_at),
        }
        for route in routes
    ]
    pending.sort(key=lambda p: (p["awaiting_since"] or "", p["action_type"]))
    return {"pending_count": len(pending), "pending": pending}


def machine_next_step(
    *, incident_id: str, device_agent_id: str, pending_count: int,
    has_diagnosis: bool,
) -> dict[str, Any]:
    """The next governed step, from platform facts only (D7).

    Its inputs are the whole of what it may know: whether an approval is
    pending and whether a diagnosis EXISTS. The diagnosis's text is not an
    input, so generated content cannot choose a step, a code or a ref.
    """
    incident_ref = [{"type": REF_INCIDENT, "id": incident_id}]
    if pending_count:
        refs = (
            [{"type": REF_DEVICE, "id": device_agent_id}] if device_agent_id
            else incident_ref
        )
        return {"code": NEXT_REVIEW_PENDING_APPROVAL, "refs": refs}
    if has_diagnosis:
        return {"code": NEXT_REVIEW_DIAGNOSIS, "refs": incident_ref}
    return {"code": NEXT_INVESTIGATE, "refs": incident_ref}


def _epoch(value: Any) -> Optional[float]:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def order_key(row) -> tuple:
    """Newest first, NULL last, the id breaking ties: one order everywhere."""
    opened = _epoch(getattr(row, "opened_at", None))
    return (opened is None, -(opened or 0.0), row.incident_id)


def child_order_key(row) -> tuple:
    """Children oldest first, as the Site Manager attached them."""
    opened = _epoch(getattr(row, "opened_at", None))
    return (opened is None, opened or 0.0, row.incident_id)


# -- the item, the list, the detail ---------------------------------------------


def machine_incident_item(
    row, *, reach, site_names: Mapping[str, str], visible_parents: frozenset,
    children: Sequence[str], view,
) -> dict[str, Any]:
    """One incident, as a machine principal may read it.

    `reach` is the reader's `incident.view` reach; the row is already one it
    may read. `children` are the children it could read ON THEIR OWN (R2).
    """
    site_id = row.site_id or ""
    holds_site = bool(reach.tenant_wide) or (
        bool(site_id) and reach.covers_site(site_id)
    )
    parent = row.parent_incident_id
    if parent and parent not in visible_parents:
        # A hidden parent is not named, and not hinted at (D7).
        parent = None
    return {
        "incident_id": row.incident_id,
        "kind": _code(row.kind, KINDS),
        "status": _code(row.status, STATUSES),
        "subsystem": _code(row.subsystem or "", SUBSYSTEMS),
        "target": {
            "device_agent_id": row.device_agent_id or "",
            "site_id": site_id,
            # The reader reads this device without holding its site: the
            # site is context, never reach (A30.25 R10).
            "site_contextual": bool(site_id) and not holds_site,
        },
        "labels": {
            "trust": TRUST_OPERATOR_SUPPLIED,
            "site_name": _text(site_names.get(site_id, ""), SITE_NAME_MAX),
        },
        "relation": {
            "parent_incident_id": parent,
            "children": list(children),
            "child_count": len(children),
        },
        # A peer-vote or inferred-domain confidence is a fact about the
        # CORRELATION, which a reader who does not hold the site is not
        # given (D4). A single device's own incident keeps its own.
        "confidence": (
            _unit(row.confidence) if holds_site or row.kind == "device" else None
        ),
        "inferred": bool(row.inferred),
        "timeline": {
            "opened_at": _iso(row.opened_at),
            "last_seen_at": _iso(row.last_seen_at),
            "resolved_at": _iso(row.resolved_at),
        },
        "components": machine_components(row.components),
        "diagnosis": machine_diagnosis(row.explanation, view),
    }


def machine_incident_list(items: Sequence[Mapping], *, now: datetime) -> dict[str, Any]:
    return {
        **stamp(CONTRACT_LIST, now),
        "returned": len(items),
        "incidents": list(items),
    }


def machine_incident_detail(
    item: Mapping, *, prior_learning: Sequence[Mapping], approvals: Mapping,
    next_step: Mapping, now: datetime,
) -> dict[str, Any]:
    return {
        **stamp(CONTRACT_DETAIL, now),
        **item,
        "prior_learning": list(prior_learning),
        "approvals": dict(approvals),
        "next_step": dict(next_step),
    }
