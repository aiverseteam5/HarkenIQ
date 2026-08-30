"""The Operational Agent: a governed actor, not a second governance model.

A0+A1 (2026-08-30). This module owns the product noun. An Operational
Agent is a **declarative bundle** over capabilities that already exist:

    identity      a named, versioned, tenant-owned row
    scope         explicit sites / device classes / devices, never a wildcard
    capabilities  references to governed capabilities, never new ones
    policy        a ceiling that can only ever TIGHTEN the tenant's own

It is configuration, never a runtime. It holds no credential (machine
identity is A3), it has no API of its own that a human lacks, and it
reaches nothing its bundle does not name.

Why the decision path lives at Central Command
----------------------------------------------
The agent's whole contribution is the evidence a device cannot see:
fleet-wide outcome rates, learned signals, cross-site patterns, the
tenant's autonomy contract, live safety state, incident diagnosis. All
of that lives at CC and is already composed by pure functions
(`build_attention`, `build_autonomy`) that a browser, an MCP tool and a
service account all read the same way. Putting the agent anywhere else
would force it to re-derive those joins and drift from what the operator
sees. So the agent evaluates HERE and executes THERE: Site Manager stays
the execution boundary and the node funnel stays the only thing that
authorizes an action.

At A3 the evaluator becomes a credentialed external caller reading the
same contracts through the same guards. Nothing in this module has to
change for that, which is the test of whether the seam is real.

What this module must never become
----------------------------------
* A second authorization model. Disposition here is a PREDICTION derived
  from the S5 contract; it grants nothing. Every proposal still passes
  `action.approve` (or the tenant's autonomy grant), then the node's
  allow-list, preconditions, stop switch, lease and blast radius.
* A second intelligence model. Nothing here scores risk, detects a
  pattern, or judges a device. Every number this module emits was
  computed by an existing capability and is carried with its source.
* A rules engine. The condition-to-remediation table below is a faithful
  projection of what the platform ALREADY does in its shipped skills and
  its A2.1 action semantics. Widening it is a product decision.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from harkeniq_cc.autonomy import (
    AUTONOMOUS,
    DENIED,
    NOT_BUDGET_MAPPED,
    REQUIRES_APPROVAL,
    SCOPE_TENANT,
)

#: Bump when a consumer would have to change to read an agent payload.
AGENT_CONTRACT_VERSION = "1"

ATTRIBUTION_PREFIX = "op-agent:"

# -- lifecycle ---------------------------------------------------------------

STATUS_DRAFT = "draft"
STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"
STATUS_RETIRED = "retired"

AGENT_STATUSES = (STATUS_DRAFT, STATUS_ACTIVE, STATUS_PAUSED, STATUS_RETIRED)

#: Only an active agent evaluates. A paused agent keeps its bundle and
#: its history; a retired one is kept for the audit record and never
#: reactivated under the same name.
EVALUATING_STATUSES = (STATUS_ACTIVE,)

# -- scope -------------------------------------------------------------------

SCOPE_SITE = "site"
SCOPE_DEVICE_CLASS = "device_class"
SCOPE_DEVICE = "device"

SCOPE_TYPES = (SCOPE_SITE, SCOPE_DEVICE_CLASS, SCOPE_DEVICE)

# -- capability bindings -----------------------------------------------------

KIND_READ = "read"
KIND_ACTION_CLASS = "action_class"
#: Reserved, and deliberately NOT accepted yet. A0 accepted this kind,
#: rendered it in the UI, validated nothing about it, and wired it to
#: nothing: no skill was installed, no directive was queued, no device
#: changed. E0.3 removed it rather than leave a capability that is
#: accepted and inert.
#:
#: Making it real is A2's "binding + deployment" and needs four things
#: that do not exist: a Console endpoint serving a skill's YAML by id
#: (today it is exposed only through the marketplace-INSTALLS feed),
#: a Central Command fetch path, per-device targeting on the InstallSkill
#: RPC (it fans out to every device on the site), and an
#: install-on-activation trigger. Deferred, not discarded.
KIND_SKILL = "skill"

CAPABILITY_KINDS = (KIND_READ, KIND_ACTION_CLASS)

#: Read capabilities an agent may be bound to. Each is an existing
#: governed CC surface with its own permission guard; binding one grants
#: nothing that the caller's RBAC does not already allow.
READ_CAPABILITIES: dict[str, str] = {
    "attention": "Ranked attention with evidence (/api/attention)",
    "autonomy": "The tenant's autonomy contract (/api/autonomy)",
    "incidents": "Open incidents and their diagnosis (/api/incidents)",
    "learning": "Learned signals and outcome evidence (/api/learning)",
    "fleet": "Fleet inventory and health (/api/fleet)",
}

#: Reads an agent cannot function without. An agent must observe the
#: condition it proposes against, and must read the governance contract
#: it claims authority from, so these are added at creation rather than
#: left to a checkbox someone forgets.
REQUIRED_READS = ("attention", "autonomy")

# -- proposal statuses -------------------------------------------------------

PROPOSAL_AWAITING = "awaiting_approval"
PROPOSAL_APPROVED = "approved"
PROPOSAL_BLOCKED = "blocked"
PROPOSAL_DENIED = "denied"
PROPOSAL_DISPATCHED = "dispatched"
PROPOSAL_COMPLETED = "completed"
PROPOSAL_FAILED = "failed"

BASIS_HUMAN = "human_approval"
BASIS_AUTONOMOUS = "autonomous_grant"


# ---------------------------------------------------------------------------
# The condition -> candidate remediation table
# ---------------------------------------------------------------------------
#
# PROVENANCE MATTERS HERE. Every row below already exists somewhere in
# the platform; this table is a projection, not a new opinion:
#
#   disk    -> IDENTIFY_LED      skills/disk-health.yaml (3 rules)
#   fan     -> COLLECT_DIAGNOSTICS  skills/fan-health.yaml
#   memory  -> COLLECT_DIAGNOSTICS  skills/memory-health.yaml
#   psu     -> COLLECT_DIAGNOSTICS  skills/psu-health.yaml
#   thermal -> FAN_RESET            skills/thermal-health.yaml (2 rules)
#   log     -> SEL_CLEAR            the A2.1 remediation for a saturated
#                                   event log; the class S5 grants at level 2
#   interface -> CLEAR_COUNTERS     R6 low-risk counter reset. Note
#                                   interface-health.yaml is deliberately
#                                   action-free for congestion; counters are
#                                   the one honest non-destructive step
#
# A subsystem absent from this table yields NO candidate. Silence is the
# correct answer for a condition the platform has no remediation for.

REMEDIATION_CANDIDATES: dict[str, list[dict[str, str]]] = {
    "disk": [{
        "action_type": "IDENTIFY_LED",
        "because": "a failing drive has to be found before it can be replaced",
        "provenance": "skills/disk-health.yaml",
    }],
    "fan": [{
        "action_type": "COLLECT_DIAGNOSTICS",
        "because": "capture the fault state before the evidence rotates away",
        "provenance": "skills/fan-health.yaml",
    }],
    "memory": [{
        "action_type": "COLLECT_DIAGNOSTICS",
        "because": "capture the fault state before the evidence rotates away",
        "provenance": "skills/memory-health.yaml",
    }],
    "psu": [{
        "action_type": "COLLECT_DIAGNOSTICS",
        "because": "capture the fault state before the evidence rotates away",
        "provenance": "skills/psu-health.yaml",
    }],
    "thermal": [{
        "action_type": "FAN_RESET",
        "because": "a stuck fan controller is the recoverable half of a thermal fault",
        "provenance": "skills/thermal-health.yaml",
    }],
    "log": [{
        "action_type": "SEL_CLEAR",
        "because": "a saturated event log hides the next fault behind the last one",
        "provenance": "A2.1 action semantics; granted at autonomy level 2",
    }],
    "interface": [{
        "action_type": "CLEAR_COUNTERS",
        "because": "reset the counter baseline so the next error rate is measurable",
        "provenance": "R6 network actions (A9 D6)",
    }],
}

#: Applied when the platform can no longer reach the device's management
#: controller while its node is still reporting. BMC_RESET is the R3a
#: action whose entire purpose is this condition.
UNREACHABLE_CANDIDATE = {
    "action_type": "BMC_RESET",
    "because": "the management controller stopped answering while the node kept reporting",
    "provenance": "R3a action semantics; granted at autonomy level 2",
}

#: Device observations that mean "the BMC is not answering". Anything
#: else, including an empty string, is NOT treated as unreachable: an
#: unobserved state must never be read as a fault (OQ-12).
UNREACHABLE_OBSERVATIONS = ("unreachable", "unreported", "degraded_link")


def attribution_key(agent_id: str, version: int) -> str:
    """The actor string an agent's work carries everywhere (design §6).

    Versioned on purpose: an outcome must name the exact configuration
    that produced it, so editing a bundle can never rewrite what an
    earlier proposal was made under.
    """
    return f"{ATTRIBUTION_PREFIX}{agent_id}@v{int(version)}"


def parse_attribution(actor: str) -> Optional[tuple[str, int]]:
    """(agent_id, version) from an attribution key, or None."""
    if not actor or not actor.startswith(ATTRIBUTION_PREFIX):
        return None
    body = actor[len(ATTRIBUTION_PREFIX):]
    if "@v" not in body:
        return None
    agent_id, _, ver = body.partition("@v")
    try:
        return agent_id, int(ver)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Scope and bindings
# ---------------------------------------------------------------------------


def resolve_scope(scopes: Iterable[Any], devices: Iterable[Any]) -> list[Any]:
    """Devices this agent may observe. Fail closed: no rows, no devices.

    The three scope types UNION (a site plus one out-of-site device is a
    legitimate assignment). What they never do is widen: a device outside
    every scope row is invisible to the agent even if the tenant owns it.
    """
    scopes = list(scopes)
    if not scopes:
        return []
    site_ids = {s.scope_ref for s in scopes if s.scope_type == SCOPE_SITE}
    classes = {
        (s.scope_ref or "").lower()
        for s in scopes
        if s.scope_type == SCOPE_DEVICE_CLASS
    }
    device_ids = {s.scope_ref for s in scopes if s.scope_type == SCOPE_DEVICE}

    selected = []
    for dev in devices:
        if dev.agent_id in device_ids:
            selected.append(dev)
            continue
        if dev.site_id in site_ids:
            selected.append(dev)
            continue
        if (getattr(dev, "device_class", "") or "server").lower() in classes:
            selected.append(dev)
    return selected


def bound_action_classes(capabilities: Iterable[Any]) -> set[str]:
    return {
        (c.capability_ref or "").upper()
        for c in capabilities
        if c.kind == KIND_ACTION_CLASS
    }


def bound_reads(capabilities: Iterable[Any]) -> set[str]:
    return {
        (c.capability_ref or "").lower()
        for c in capabilities
        if c.kind == KIND_READ
    }


def bound_skills(capabilities: Iterable[Any]) -> set[str]:
    """Skills bound to this agent. Always empty until A2 (see KIND_SKILL).

    Kept so the agent view has a stable shape across the change rather
    than gaining and losing a field.
    """
    return {c.capability_ref for c in capabilities if c.kind == KIND_SKILL}


# ---------------------------------------------------------------------------
# Governance: the agent's disposition is the tenant's, only ever narrower
# ---------------------------------------------------------------------------


def effective_disposition(
    agent, class_row: dict, stop_switch_active: bool = False,
) -> dict[str, Any]:
    """What this agent may do with this action class, and why.

    Starts from the S5 contract's disposition for the tenant and applies
    the agent's own ceiling. Every rule here can only TIGHTEN: an agent
    is never granted something the tenant is not, which is what keeps
    "one autonomy model" true when there are many agents.

    Two translations, because the contract answers a narrower question
    than an actor needs to:

    * `not_budget_mapped` is a statement about AUTONOMY, not about
      permission. No level grants the class, so it can never run
      unattended, but a named human may still approve it exactly as they
      approve one the node proposed. Reading it as "forbidden" would
      have silently removed IDENTIFY_LED, COLLECT_DIAGNOSTICS and
      FAN_RESET from what an agent may ever ask for, which is most of
      the low-risk work an operator would actually delegate.

    * A tenant stop switch denies EVERY class, mapped or not. The
      contract marks the mapped ones denied; an unmapped class never
      reaches that branch, and proposing into a stopped tenant would
      spend a human's decision on work the node will refuse anyway
      (A10.3: approval never overrides a safety gate).
    """
    disposition = class_row.get("disposition", REQUIRES_APPROVAL)
    reason = class_row.get("disposition_reason", "")
    blocking = list(class_row.get("blocking_conditions") or [])
    grant_level = class_row.get("granted_at_level")

    if stop_switch_active and disposition != DENIED:
        return {
            "disposition": DENIED,
            "disposition_reason": (
                "the tenant stop switch denies all action; a human decision "
                "cannot override it"
            ),
            "blocking_conditions": blocking + [{
                "code": "stop_switch_active",
                "detail": "the tenant stop switch denies all action",
                "scope": SCOPE_TENANT,
            }],
            "authorization_basis": BASIS_HUMAN,
        }

    if disposition == NOT_BUDGET_MAPPED:
        return {
            "disposition": REQUIRES_APPROVAL,
            "disposition_reason": (
                "no autonomy level maps this class, so it always needs a "
                "named human. Mapping it is a product decision, not an "
                "evidence threshold."
            ),
            "blocking_conditions": blocking,
            "authorization_basis": BASIS_HUMAN,
        }

    if disposition == AUTONOMOUS:
        if getattr(agent, "require_approval_always", True):
            disposition = REQUIRES_APPROVAL
            reason = (
                "this agent is configured to ask for a human decision on "
                "every action, even where the tenant grants autonomy"
            )
            blocking.append({
                "code": "agent_requires_approval",
                "detail": reason,
                "scope": SCOPE_TENANT,
            })
        elif grant_level is not None and int(
            getattr(agent, "autonomy_ceiling", 0) or 0
        ) < int(grant_level):
            disposition = REQUIRES_APPROVAL
            reason = (
                f"the tenant grants this class at level {grant_level}, but "
                f"this agent's ceiling is level "
                f"{int(getattr(agent, 'autonomy_ceiling', 0) or 0)}"
            )
            blocking.append({
                "code": "agent_ceiling_below_grant",
                "detail": reason,
                "scope": SCOPE_TENANT,
            })

    return {
        "disposition": disposition,
        "disposition_reason": reason,
        "blocking_conditions": blocking,
        "authorization_basis": (
            BASIS_AUTONOMOUS if disposition == AUTONOMOUS else BASIS_HUMAN
        ),
    }


def _suppressed_sites(class_row: dict) -> set[str]:
    """Sites with a live fault-domain suppression for this class.

    Suppression fences a fault domain, so it is contextual rather than
    class-wide (S5 says so explicitly). An agent must still evaluate it
    per target, and the honest evaluation at fleet granularity is: do
    not propose into a site that is currently suppressing conclusions.
    """
    return {
        d.get("site_id", "")
        for d in (class_row.get("safety") or {}).get("suppressed_domains", [])
        if d.get("site_id")
    }


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def _observed_conditions(device, incidents: list[dict]) -> list[dict]:
    """What the platform already observed about this device.

    Nothing is computed here. Incidents come from the Site Manager's own
    consolidation; the unreachable condition comes from the fleet
    observation field the poller writes. An empty list means the
    platform saw nothing worth acting on, which is a real answer.
    """
    conditions: list[dict] = []
    observation = (getattr(device, "observation", "") or "").lower()
    if observation in UNREACHABLE_OBSERVATIONS:
        conditions.append({
            "kind": "unreachable",
            "subsystem": "bmc",
            "detail": f"device observation is {observation!r}",
            "incident_ids": [],
        })
    for inc in incidents:
        subsystem = (inc.get("subsystem") or "").lower()
        if not subsystem:
            continue
        conditions.append({
            "kind": "incident",
            "subsystem": subsystem,
            "detail": inc.get("title") or f"open {subsystem} incident",
            "incident_ids": [inc.get("incident_id")],
            "diagnosis": bool(inc.get("diagnosis") or inc.get("explanation")),
        })
    return conditions


def _candidates_for(condition: dict) -> list[dict]:
    if condition["kind"] == "unreachable":
        return [UNREACHABLE_CANDIDATE]
    return REMEDIATION_CANDIDATES.get(condition["subsystem"], [])


def _rationale(agent_name: str, device, condition: dict, candidate: dict,
               class_row: dict) -> str:
    """One sentence an operator can act on without opening anything else."""
    ev = class_row.get("evidence") or {}
    rate = ev.get("success_rate")
    device_label = getattr(device, "agent_name", "") or device.agent_id
    detail = condition["detail"]
    # Incident titles already name their device; repeating it produced
    # "observed X: fan CRITICAL on X" on the live stack.
    where = "" if device_label and device_label in detail else f" on {device_label}"
    head = (
        f"{agent_name} observed {detail}{where} and recommends "
        f"{candidate['action_type'].replace('_', ' ').lower()}: "
        f"{candidate['because']}."
    )
    if rate is not None:
        head += (
            f" This class has succeeded {rate:.0%} of the time across "
            f"{ev.get('executions', 0)} executions in this tenant."
        )
    elif ev.get("executions"):
        head += (
            f" The tenant has only {ev['executions']} recorded execution(s) "
            f"of this class, too few to judge a success rate."
        )
    else:
        head += " This tenant has no recorded outcome for this class yet."
    return head


def evaluate(
    *,
    agent,
    scopes: Iterable[Any],
    capabilities: Iterable[Any],
    devices: Iterable[Any],
    incidents_by_device: dict[str, list[dict]],
    autonomy_contract: dict,
    attention_by_device: Optional[dict[str, dict]] = None,
    open_dedupe_keys: Iterable[str] = (),
    proposals_today: int = 0,
    now: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    """One evaluation pass for one agent. Pure: no I/O, no clock of its own.

    Returns proposal payloads ready to persist. Ordering is deterministic
    (attention rank, then device id) so two runs over the same state
    produce the same proposals in the same order.
    """
    now = now or datetime.now(timezone.utc)
    attention_by_device = attention_by_device or {}
    open_keys = set(open_dedupe_keys)
    actor = attribution_key(agent.id, agent.version)

    allowed_classes = bound_action_classes(capabilities)
    if not allowed_classes:
        return []

    class_rows = {
        row["action_type"]: row
        for row in autonomy_contract.get("action_classes", [])
    }
    in_scope = resolve_scope(scopes, devices)
    if not in_scope:
        return []
    stop_switch_active = bool(
        (autonomy_contract.get("posture") or {})
        .get("stop_switch", {})
        .get("active", False)
    )

    budget_left = max(
        0, int(getattr(agent, "max_proposals_per_day", 0) or 0) - int(proposals_today)
    )

    def _rank(dev) -> tuple:
        item = attention_by_device.get(dev.agent_id) or {}
        return (item.get("rank", 10**6), dev.agent_id)

    proposals: list[dict[str, Any]] = []
    for device in sorted(in_scope, key=_rank):
        if budget_left <= 0:
            break
        incidents = incidents_by_device.get(device.agent_id, [])
        conditions = _observed_conditions(device, incidents)
        if not conditions:
            continue

        made_for_device = False
        for condition in conditions:
            if made_for_device:
                break
            for candidate in _candidates_for(condition):
                action_type = candidate["action_type"]
                if action_type not in allowed_classes:
                    continue
                class_row = class_rows.get(action_type)
                if class_row is None:
                    # The contract does not describe this class, which
                    # means the executor does not have it. Never propose
                    # into a class the platform cannot run.
                    continue
                # The key names the CONDITION, not just the class: a
                # new incident is new work, but the same open incident
                # must not be re-proposed every pass. Without the
                # condition, a permanently-refused action came back on
                # the next cycle forever (live-stack finding).
                condition_ref = (
                    (condition.get("incident_ids") or [None])[0]
                    or condition["kind"]
                )
                dedupe_key = (
                    f"{agent.id}:{device.agent_id}:{action_type}:{condition_ref}"
                )
                if dedupe_key in open_keys:
                    continue

                verdict = effective_disposition(
                    agent, class_row, stop_switch_active,
                )
                blocking = verdict["blocking_conditions"]
                disposition = verdict["disposition"]

                if device.site_id in _suppressed_sites(class_row):
                    disposition = REQUIRES_APPROVAL
                    blocking = blocking + [{
                        "code": "site_suppressed",
                        "detail": (
                            "a fault domain at this site is suppressing "
                            "correlated conclusions; a human should look "
                            "before anything runs here"
                        ),
                        "scope": "site",
                        "site_id": device.site_id,
                    }]

                if disposition == DENIED:
                    status = PROPOSAL_BLOCKED
                elif disposition == AUTONOMOUS:
                    status = PROPOSAL_APPROVED
                else:
                    status = PROPOSAL_AWAITING

                attention = attention_by_device.get(device.agent_id) or {}
                evidence = {
                    "observed": condition["detail"],
                    "condition_kind": condition["kind"],
                    "subsystem": condition["subsystem"],
                    "incident_ids": [
                        i for i in condition.get("incident_ids", []) if i
                    ],
                    "has_diagnosis": bool(condition.get("diagnosis")),
                    "remediation_provenance": candidate["provenance"],
                    "attention": {
                        "rank": attention.get("rank"),
                        "band": attention.get("band"),
                        "driver": attention.get("attention_driver"),
                        "risk_score": attention.get("risk_score"),
                    } if attention else None,
                    "outcome_evidence": class_row.get("evidence"),
                    "learned_signals": class_row.get("learning") or [],
                    "device": {
                        "vendor": getattr(device, "vendor", ""),
                        "model": getattr(device, "model", ""),
                        "device_class": getattr(device, "device_class", "server"),
                        "health": getattr(device, "health", ""),
                        "observation": getattr(device, "observation", ""),
                    },
                    "contract_version": autonomy_contract.get("contract_version"),
                    "evaluated_at": now.isoformat(),
                }

                proposals.append({
                    "tenant_id": agent.tenant_id,
                    "agent_id": agent.id,
                    "actor": actor,
                    "agent_version": agent.version,
                    "site_id": device.site_id,
                    "device_agent_id": device.agent_id,
                    "action_type": action_type,
                    "params": {"reason": candidate["because"]},
                    "rationale": _rationale(
                        agent.name, device, condition, candidate, class_row,
                    ),
                    "evidence": evidence,
                    "disposition": disposition,
                    "disposition_reason": (
                        verdict["disposition_reason"]
                        or (blocking[0]["detail"] if blocking else "")
                    ),
                    "blocking_conditions": blocking,
                    "authorization_basis": (
                        BASIS_AUTONOMOUS if disposition == AUTONOMOUS
                        else BASIS_HUMAN
                    ),
                    "status": status,
                    "decided_by": (
                        f"autonomy:level-{class_row.get('granted_at_level')}"
                        if disposition == AUTONOMOUS else ""
                    ),
                    "decided_at": now if disposition == AUTONOMOUS else None,
                    "dedupe_key": dedupe_key,
                })
                open_keys.add(dedupe_key)
                budget_left -= 1
                made_for_device = True
                break

    return proposals


# ---------------------------------------------------------------------------
# The agent view: the questions an operator actually asks
# ---------------------------------------------------------------------------


def agent_view(
    *,
    agent,
    scopes: Iterable[Any],
    capabilities: Iterable[Any],
    devices: Iterable[Any],
    autonomy_contract: dict,
    proposals: Iterable[Any] = (),
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """One agent, answered the way an operator asks it.

    What is this agent, what can it see, what can it do, what may it do
    without me, why, what needs my approval, what did it do, what
    happened, what did it learn. Every answer is composed from the same
    governed contracts the Console and a future MCP caller read; nothing
    here is a display-only field.
    """
    now = now or datetime.now(timezone.utc)
    scopes = list(scopes)
    capabilities = list(capabilities)
    proposals = list(proposals)
    in_scope = resolve_scope(scopes, devices)
    class_rows = {
        row["action_type"]: row
        for row in autonomy_contract.get("action_classes", [])
    }
    stop_switch_active = bool(
        (autonomy_contract.get("posture") or {})
        .get("stop_switch", {})
        .get("active", False)
    )

    can_do: list[dict[str, Any]] = []
    for action_type in sorted(bound_action_classes(capabilities)):
        row = class_rows.get(action_type)
        if row is None:
            can_do.append({
                "action_type": action_type,
                "known_to_executor": False,
                "disposition": DENIED,
                "disposition_reason": (
                    "no executor on this platform implements this action class"
                ),
                "blocking_conditions": [],
                "risk": None,
                "evidence": None,
                "learning": [],
                "advancement": None,
                "requires_approval": True,
            })
            continue
        verdict = effective_disposition(agent, row, stop_switch_active)
        can_do.append({
            "action_type": action_type,
            "known_to_executor": True,
            "risk": row.get("risk"),
            "granted_at_level": row.get("granted_at_level"),
            "never_budget_grantable": row.get("never_budget_grantable"),
            "tenant_disposition": row.get("disposition"),
            "disposition": verdict["disposition"],
            "disposition_reason": verdict["disposition_reason"],
            "blocking_conditions": verdict["blocking_conditions"],
            "requires_approval": verdict["disposition"] != AUTONOMOUS,
            "authorization_basis": verdict["authorization_basis"],
            "evidence": row.get("evidence"),
            "learning": row.get("learning") or [],
            "advancement": row.get("advancement"),
        })

    by_status: dict[str, int] = {}
    for p in proposals:
        by_status[p.status] = by_status.get(p.status, 0) + 1

    settled = [p for p in proposals if p.outcome]
    succeeded = [p for p in settled if p.outcome == "SUCCESS"]

    return {
        "contract_version": AGENT_CONTRACT_VERSION,
        "generated_at": now.isoformat(),
        "agent": {
            "id": agent.id,
            "tenant_id": agent.tenant_id,
            "name": agent.name,
            "description": agent.description,
            "status": agent.status,
            "version": agent.version,
            "actor": attribution_key(agent.id, agent.version),
            "species": "agent",
            "autonomy_ceiling": agent.autonomy_ceiling,
            "require_approval_always": agent.require_approval_always,
            "max_proposals_per_day": agent.max_proposals_per_day,
            "created_by": agent.created_by,
            "created_at": agent.created_at.isoformat() if agent.created_at else None,
            "activated_by": agent.activated_by,
            "activated_at": (
                agent.activated_at.isoformat() if agent.activated_at else None
            ),
            "last_evaluated_at": (
                agent.last_evaluated_at.isoformat()
                if agent.last_evaluated_at else None
            ),
            "evaluating": agent.status in EVALUATING_STATUSES,
        },
        # "What can it see?"
        "scope": {
            "rules": [
                {"scope_type": s.scope_type, "scope_ref": s.scope_ref}
                for s in scopes
            ],
            "device_count": len(in_scope),
            "devices": [
                {
                    "agent_id": d.agent_id,
                    "agent_name": d.agent_name,
                    "site_id": d.site_id,
                    "device_class": getattr(d, "device_class", "server"),
                    "health": d.health,
                    "observation": d.observation,
                }
                for d in in_scope
            ],
            "reads": sorted(bound_reads(capabilities)),
            "explicit": bool(scopes),
            "statement": (
                f"{len(in_scope)} device(s) in scope"
                if scopes else
                "No scope assigned: this agent can see nothing until a site, "
                "device class or device is bound to it."
            ),
        },
        # "What can it do?" / "What may it do without me?" / "Why?"
        "capabilities": {
            "action_classes": can_do,
            "skills": sorted(bound_skills(capabilities)),
            "autonomous_now": sorted(
                c["action_type"] for c in can_do
                if c["disposition"] == AUTONOMOUS
            ),
            "needs_approval": sorted(
                c["action_type"] for c in can_do
                if c["disposition"] == REQUIRES_APPROVAL
            ),
            "denied": sorted(
                c["action_type"] for c in can_do if c["disposition"] == DENIED
            ),
        },
        # "What did it do?" / "What happened?"
        "activity": {
            "by_status": by_status,
            "awaiting_approval": by_status.get(PROPOSAL_AWAITING, 0),
            "blocked": by_status.get(PROPOSAL_BLOCKED, 0),
            "executed": len(settled),
            "succeeded": len(succeeded),
            "success_rate": (
                round(len(succeeded) / len(settled), 4) if settled else None
            ),
        },
    }
