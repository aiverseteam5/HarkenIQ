"""The machine Attention contract (spec A30.35, A6-4B2-2).

`GET /api/attention/` is the one read every Operational Agent is required to
hold, and until B2-2 it answered an agent with the Console's payload. That
payload was composed over the whole tenant -- every site's outcomes folded
into a vendor/model cohort rate that decided a device's band, basis, driver
and rank for any device with little history of its own -- so what an agent
was shown about its OWN devices moved with sites it cannot see (B2-F5). It
also carried incident ids, titles and kinds read under `fleet.view`, which
an agent without the `incidents` binding may not read (B2-F4).

The fix is in the INPUTS, not here: `governance.load_machine_attention`
selects everything the one composer folds from what the machine may read,
before anything is computed (A30.34 D2). This module only says what may
leave: it BUILDS ITS ANSWER BY NAMING THE FIELDS IT MAY PASS (A25.9). It
reads named fields of the composer's answer and of the device's fleet row,
and never spreads or strips a composer dict, so nothing the Console's
payload grows can arrive here by default.

WHAT A STRING IS, BY POSITION (A30.34 D5). Every string leaf is a closed
code, an opaque id, a timestamp, or free text inside an object whose
`trust` names its class. Machine Attention carries no generated text.

Pure: no I/O.
"""

from __future__ import annotations

import re
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Iterable, Mapping, Optional

from harkeniq_cc.attention import (
    DRIVER_AWAITING_APPROVAL,
    DRIVER_CURRENT_FAILURE,
    DRIVER_DEGRADED,
    DRIVER_INSUFFICIENT,
    DRIVER_PREDICTED_RISK,
)
from harkeniq_cc.freshness import UNKNOWN as FRESHNESS_UNKNOWN
from harkeniq_cc.freshness import freshness_state
from harkeniq_cc.incident_projection import (
    COMPONENT_TEXT_MAX,
    KINDS,
    NEXT_REVIEW_DIAGNOSIS,
    NEXT_REVIEW_PENDING_APPROVAL,
    REF_DEVICE,
    REF_INCIDENT,
    SITE_NAME_MAX,
    SUBSYSTEMS,
    _code,
    _iso,
    _text,
    _unit,
    machine_approvals,
    machine_prior_learning,
)
from harkeniq_cc.receipts import VIEW_MACHINE
from harkeniq_cc.trust import TRUST_OPERATOR_SUPPLIED, TRUST_UNTRUSTED_TELEMETRY
from harkeniq_cc.warranty.base import warranty_status

CONTRACT = "attention"
CONTRACT_VERSION = "1"

# -- closed vocabularies ------------------------------------------------------
# Each maps what it does not recognise to `other`, never echoing a value
# nobody vetted.

DEVICE_CLASSES = frozenset({"server", "switch"})
#: The Site Manager's health words (`worst_health`, lower-cased), plus the
#: verdict vocabulary a device may also report.
HEALTHS = frozenset({"ok", "healthy", "trending", "warning", "critical", "unknown"})
#: The Site Manager's observation states (`observation_state`), plus unknown.
OBSERVATIONS = frozenset({"observed", "stale", "unobserved", "unknown"})
DRIVERS = frozenset({
    DRIVER_CURRENT_FAILURE, DRIVER_AWAITING_APPROVAL, DRIVER_DEGRADED,
    DRIVER_PREDICTED_RISK, DRIVER_INSUFFICIENT,
})
BANDS = frozenset({"high", "medium", "low", "insufficient_data"})
#: A machine is never scored on a cohort (D2): its basis is its own history
#: or the absence of one. `cohort_prior` is not in this set on purpose.
BASES = frozenset({"device_history", "insufficient_data"})
CVE_SEVERITIES = frozenset({"critical", "high", "medium", "low", "none", "unknown"})
WARRANTY_STATES = frozenset({"active", "expiring", "expired", "unknown"})
PATTERN_TYPES = frozenset({"batch_failure", "anomaly", "reliability", "cross_site_batch"})

# -- the next step (D7, Attention half) -----------------------------------------
# The composer's own capability words. Never `propose_action`.

NEXT_PLAN_FIRMWARE = "plan_firmware_remediation"
NEXT_REVIEW_INCIDENT = "review_incident"
NEXT_INVESTIGATE_DEVICE = "investigate_device"
NEXT_COLLECT_EVIDENCE = "collect_evidence"
NEXT_MONITOR = "monitor"
NEXT_STEP_CODES = frozenset({
    NEXT_REVIEW_PENDING_APPROVAL, NEXT_PLAN_FIRMWARE, NEXT_REVIEW_DIAGNOSIS,
    NEXT_REVIEW_INCIDENT, NEXT_INVESTIGATE_DEVICE, NEXT_COLLECT_EVIDENCE,
    NEXT_MONITOR,
})
REF_CVE = "cve"

#: A CVE id, and nothing else, is published as an id. The feed is imported
#: by an operator and only stripped at import, so anything else is text.
CVE_ID = re.compile(r"CVE-\d{4}-\d{4,}")

# -- bounds -------------------------------------------------------------------

DEVICE_NAME_MAX = 255
VENDOR_MAX = 64
MODEL_MAX = 255
VERSION_MAX = 128
#: `cc_fleet_patterns.description` is 512 wide.
DESCRIPTION_MAX = 512


def _lower_code(value: Any, allowed: frozenset) -> str:
    return _code(value.lower() if isinstance(value, str) else value, allowed)


# -- the pieces ---------------------------------------------------------------


def machine_cve(entry: Mapping) -> dict[str, Any]:
    """One CVE match on this device. The installed component and version
    are the device's own report, so they sit in a telemetry envelope."""
    cve_id = entry.get("cve_id")
    return {
        "cve_id": (
            cve_id if isinstance(cve_id, str) and CVE_ID.fullmatch(cve_id) else None
        ),
        "severity": _lower_code(entry.get("severity"), CVE_SEVERITIES),
        "fix_available": bool(entry.get("fixed_version")),
        "installed": {
            "trust": TRUST_UNTRUSTED_TELEMETRY,
            "component": _text(entry.get("component"), COMPONENT_TEXT_MAX),
            "version": _text(entry.get("version"), VERSION_MAX),
        },
    }


def _cve_order(cve: Mapping) -> tuple:
    return (
        cve["cve_id"] is None, cve["cve_id"] or "", cve["severity"],
        cve["installed"]["component"], cve["installed"]["version"],
    )


def machine_pattern(entry: Mapping) -> dict[str, Any]:
    """A fleet pattern about this device's cohort, AFTER S4 projected it for
    this reader. The description interpolates the BMC-reported vendor and
    model (the D5 composition rule). No id, no evidence, no counts."""
    return {
        "pattern_type": _code(entry.get("pattern_type"), PATTERN_TYPES),
        "description": {
            "trust": TRUST_UNTRUSTED_TELEMETRY,
            "text": _text(entry.get("description"), DESCRIPTION_MAX),
        },
        "confidence": _unit(entry.get("confidence")),
    }


def _instant(value: Any) -> Optional[float]:
    stamp = _iso(value)
    return datetime.fromisoformat(stamp).timestamp() if stamp else None


def machine_incidents(entries: Iterable[Mapping], *, held: bool) -> dict[str, Any]:
    """Open incidents on this device, only where they are this machine's.

    D3: incident content needs the machine's CURRENT `incident.view`
    coverage of the device. Without it the answer is exactly
    ``{"held": false}`` -- UNKNOWN, never "none": no id, no count, no kind.
    With it, what is open, newest first -- no title, no diagnosis content,
    no correlation.
    """
    if not held:
        return {"held": False}
    rows = [
        {
            "incident_id": str(entry.get("incident_id") or ""),
            "kind": _code(entry.get("kind"), KINDS),
            "subsystem": _code(entry.get("subsystem") or "", SUBSYSTEMS),
            "diagnosed": bool(entry.get("diagnosis")),
            "opened_at": _iso(entry.get("opened_at")),
        }
        for entry in entries
    ]
    rows.sort(key=lambda r: (
        _instant(r["opened_at"]) is None, -(_instant(r["opened_at"]) or 0.0),
        r["incident_id"],
    ))
    return {"held": True, "open_count": len(rows), "open": rows}


def _pending(entry: Mapping) -> SimpleNamespace:
    """A pending node action, as the ONE bounded-approval builder reads one
    (B2-1's `machine_approvals`): its class and since when. The handle the
    composer carries is not passed on."""
    return SimpleNamespace(
        action_type=entry.get("action_type"), routed_at=entry.get("routed_at"),
    )


def machine_warranty_state(warranty: Any, now: datetime) -> str:
    if not isinstance(warranty, Mapping):
        return "unknown"
    end_date = warranty.get("end_date")
    if not isinstance(end_date, str):
        return "unknown"
    return _code(warranty_status(end_date, now=now), WARRANTY_STATES)


def machine_freshness(row: Any, now: datetime) -> dict[str, Any]:
    """D8: the device's own reading, through the ONE rule `/runtime` asks.
    `last_seen_at` is when the site last heard from it; `snapshot_at` is
    when Central Command last copied the row. Context, never authority."""
    if row is None:
        return {"state": FRESHNESS_UNKNOWN, "last_seen_at": None, "snapshot_at": None}
    last_seen = getattr(row, "last_seen_at", None)
    return {
        "state": freshness_state(last_seen, now),
        "last_seen_at": _iso(last_seen),
        "snapshot_at": _iso(getattr(row, "snapshot_at", None)),
    }


def machine_next_step(
    capability: Any, *, device_agent_id: str, cves: Iterable[Mapping],
    incidents: Mapping,
) -> dict[str, Any]:
    """`{code, refs}` (D7, Attention half).

    The code is the ONE composer's own decision over this machine's inputs
    (`attention._recommend`), so there is no second rule to drift from it.
    Refs are ids shown in THIS item: the device, CVE-shaped ids with a fix,
    held incidents. Never an action id, prose, or an approval answer -- B1
    discovery is the one answer on approval and addressability.
    """
    code = _code(capability, NEXT_STEP_CODES)
    if code in (NEXT_REVIEW_PENDING_APPROVAL, NEXT_INVESTIGATE_DEVICE):
        refs = [{"type": REF_DEVICE, "id": device_agent_id}] if device_agent_id else []
    elif code == NEXT_PLAN_FIRMWARE:
        ids = sorted({c["cve_id"] for c in cves if c["fix_available"] and c["cve_id"]})
        refs = [{"type": REF_CVE, "id": cve_id} for cve_id in ids]
    elif code == NEXT_REVIEW_DIAGNOSIS:
        refs = [
            {"type": REF_INCIDENT, "id": i["incident_id"]}
            for i in incidents.get("open", []) if i["diagnosed"]
        ]
    elif code == NEXT_REVIEW_INCIDENT:
        refs = [
            {"type": REF_INCIDENT, "id": i["incident_id"]}
            for i in incidents.get("open", [])
        ]
    else:
        refs = []
    return {"code": code, "refs": refs}


# -- the item and the answer ----------------------------------------------------


def machine_attention_item(
    item: Mapping, *, row: Any, held: bool, fleet, now: datetime,
) -> dict[str, Any]:
    """One device, as a machine principal may read it.

    `item` is the composer's answer for this device, computed over the
    machine's own inputs. `row` is the device's fleet row (freshness),
    `held` whether its incidents are this machine's (D3), and `fleet` the
    route's reach (a site the machine reads a device at without holding it
    is context, never reach: A30.25 R10).
    """
    agent_id = str(item.get("agent_id") or "")
    site_id = str(item.get("site_id") or "")
    holds_site = bool(fleet.tenant_wide) or (
        bool(site_id) and fleet.covers_site(site_id)
    )
    evidence = item.get("evidence") or {}
    state = item.get("current_state") or {}
    cves = sorted(
        (machine_cve(entry) for entry in evidence.get("cves") or []), key=_cve_order,
    )
    incidents = machine_incidents(state.get("open_incidents") or [], held=held)
    return {
        "order": int(item["rank"]),
        "target": {
            "device_agent_id": agent_id,
            "site_id": site_id,
            "site_contextual": bool(site_id) and not holds_site,
        },
        "labels": {
            "trust": TRUST_OPERATOR_SUPPLIED,
            "site_name": _text(item.get("site_name"), SITE_NAME_MAX),
        },
        # The node names itself at registration and a BMC reports vendor and
        # model: the device's word, not the platform's.
        "reported": {
            "trust": TRUST_UNTRUSTED_TELEMETRY,
            "device_name": _text(item.get("agent_name"), DEVICE_NAME_MAX),
            "vendor": _text(item.get("vendor"), VENDOR_MAX),
            "model": _text(item.get("model"), MODEL_MAX),
        },
        "device_class": _lower_code(item.get("device_class"), DEVICE_CLASSES),
        "health": _lower_code(item.get("health"), HEALTHS),
        "observation": _lower_code(item.get("observation"), OBSERVATIONS),
        "driver": _code(item.get("attention_driver"), DRIVERS),
        "risk": {
            "basis": _code((item.get("confidence") or {}).get("basis"), BASES),
            "band": _code(item.get("band"), BANDS),
        },
        "cves": cves,
        "warranty_state": machine_warranty_state(evidence.get("warranty"), now),
        "prior_learning": machine_prior_learning(evidence.get("learned_signals") or []),
        "fleet_patterns": [
            machine_pattern(entry) for entry in evidence.get("fleet_patterns") or []
        ],
        "incidents": incidents,
        "approvals": machine_approvals(
            _pending(entry) for entry in state.get("pending_approvals") or []
        ),
        "freshness": machine_freshness(row, now),
        "next_step": machine_next_step(
            (item.get("recommended_next") or {}).get("capability"),
            device_agent_id=agent_id, cves=cves, incidents=incidents,
        ),
    }


def machine_attention(composition, *, now: datetime) -> dict[str, Any]:
    """The whole answer: the stamp (D1), `returned`, and the items in the
    composer's order over the machine's inputs. Nothing else -- no site
    rollup, no summary total, no tenant id."""
    items = [
        machine_attention_item(
            item,
            row=composition.devices.get(
                (str(item.get("site_id") or ""), str(item.get("agent_id") or ""))
            ),
            held=str(item.get("agent_id") or "") in composition.held,
            fleet=composition.fleet,
            now=now,
        )
        for item in composition.composed["items"]
    ]
    return {
        "view": VIEW_MACHINE,
        "contract": CONTRACT,
        "contract_version": CONTRACT_VERSION,
        "as_of": now.isoformat(),
        "returned": len(items),
        "items": items,
    }
