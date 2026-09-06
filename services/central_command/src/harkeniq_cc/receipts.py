"""A25: the machine-facing lifecycle receipt.

A projection over canonical state. It reads six sources and invents no
seventh: `cc_agent_submissions`, `cc_agent_proposals`, the E0.1 approval
ledger, the proposal's own dispatch fields, `cc_outcome_history`, and a
terminality rule derived from all of them.

THE LAYERS STAY SEPARATE (A25.4). There is no single `status` string, and
adding one later would be a regression rather than a convenience: proposal
status is not approval state is not execution state is not outcome.
`approved` does not mean executed, and is not terminal — the per-agent
budget can return it to `awaiting_approval`, which is a transition a
machine consumer must be able to observe rather than have smoothed away.

WHAT IS ABSENT, AND WHY IT IS ABSENT HERE RATHER THAN AT THE ROUTE
------------------------------------------------------------------
`evaluate_completion` returns approver emails and a denier's identity, and
`proposal_dict` returns `decided_by`. Both are correct for the Console.
Neither may reach a machine principal (A25.3), so this module builds the
approval block by NAMING the fields it may pass rather than by removing
the ones it may not. A projection that filtered by exclusion would leak
the next field somebody adds upstream.

Also absent, for the same reason: raw evidence, executable parameters,
group membership, policy names, and any other agent's work.

THE HISTORICAL RECEIPT (A25.2)
------------------------------
Operational reads are current-authority. The one exception is an agent
reading the receipt of a submission it made itself, after its scope has
narrowed or been revoked. That is historical transaction attribution, not
operational authority, so the narrowed receipt carries lifecycle facts and
NOTHING about the estate — no device, no site, no fleet state. The
response says which of the two it is, so a runtime can tell a narrowed
answer from a complete one instead of inferring it from missing fields.

A submission id is not a bearer credential: the caller must already be the
agent that created the submission, which the route establishes before
anything here runs.
"""

from __future__ import annotations

from typing import Any, Optional

# ---------------------------------------------------------------------------
# Terminality (A25.4)
# ---------------------------------------------------------------------------

#: Proposal states from which nothing further can happen, and the layer
#: that ended it. Walked against the documented lifecycle, not assumed.
#:
#: `approved` is deliberately ABSENT: A2's per-agent budget can return it
#: to `awaiting_approval` (`withhold_unattended`), so treating it as final
#: would let a cache conceal exactly the transition A25.7 protects.
TERMINAL_STATES: dict[str, str] = {
    "completed": "outcome",
    "failed": "outcome",
    "denied": "approval",
    "blocked": "governance",
}

#: What the caller may be told about why a receipt is narrowed.
VIEW_FULL = "full"
VIEW_RECEIPT = "historical_receipt"
#: Every machine projection says so, in one word, in one place.
VIEW_MACHINE = "machine"


def is_terminal(status: str) -> bool:
    return status in TERMINAL_STATES


def _iso(value) -> Optional[str]:
    return value.isoformat() if value else None


def _bounded(text: str, limit: int = 512) -> str:
    """Refusal and failure reasons are bounded (A25.2)."""
    return (text or "")[:limit]


# ---------------------------------------------------------------------------
# Blocks
# ---------------------------------------------------------------------------


def submission_block(row) -> dict[str, Any]:
    """The agent's own act. Always readable by the agent that made it."""
    if row is None:
        return {}
    return {
        "submission_id": row.id,
        "idempotency_key": row.idempotency_key,
        "accepted": bool(row.proposal_id),
        "code": row.code or "",
        "reason": _bounded(row.reason),
        "submitted_at": _iso(row.created_at),
    }


def proposal_block(proposal, *, full: bool) -> dict[str, Any]:
    """Lifecycle state, and estate detail ONLY under current authority."""
    if proposal is None:
        return {}
    block = {
        "proposal_id": proposal.id,
        "status": proposal.status,
        "disposition": proposal.disposition,
        "disposition_reason": _bounded(proposal.disposition_reason),
        "created_at": _iso(proposal.created_at),
    }
    if full:
        # ESTATE IDENTITY, and only that. A25.2 withholds every line of
        # this once current authority is gone, which is why it is added
        # here rather than stripped somewhere else.
        #
        # Note what is NOT here even under full authority, because these
        # are machine projections and `full` is a question about the
        # ESTATE, not about execution internals:
        #
        #   params                -- server-derived and never authored by
        #                            the agent; it asked for a candidate,
        #                            not for a payload
        #   authorization_basis   -- how the platform justified running
        #                            this is governance's business
        #   dispatch_reason       -- internal delivery detail
        #
        # Conflating the two is what let the machine list serialize an
        # executable payload to an external runtime.
        block.update({
            "action_type": proposal.action_type,
            "device_agent_id": proposal.device_agent_id,
            "site_id": proposal.site_id,
            # Governance's own reasons for withholding the work. Useful to
            # the submitter, and about the decision rather than the
            # execution.
            "blocking_conditions": proposal.blocking_conditions or [],
        })
    return block


def approval_block(completion: Optional[dict], proposal) -> dict[str, Any]:
    """That governance happened — never who performed it (A25.3).

    Built by naming what may pass. `evaluate_completion` also returns
    `approvers` (with emails) and `denied_by`; those are correct for the
    Console and must never reach a machine principal, so they are simply
    not named here.
    """
    required = bool(
        proposal is not None
        and (proposal.authorization_basis or "") == "human_approval"
    )
    block = {
        "required": required,
        "state": "not_required",
        "granted_count": 0,
        "required_count": 0,
        "decided_at": _iso(getattr(proposal, "decided_at", None)),
    }
    if completion:
        block.update({
            "state": completion.get("state", "pending"),
            "granted_count": int(completion.get("received", 0)),
            "required_count": int(completion.get("required", 0)),
        })
    elif required:
        block["state"] = "pending"
    return block


def execution_block(proposal, *, full: bool = False) -> dict[str, Any]:
    """Whether it reached a site — not the internal handle that took it.

    `directive_id` is Central Command's link to a Site Manager record and
    is of no use to the submitter; D3 keeps internal correlation handles
    out of the external contract, so this reports only that a directive
    exists.
    """
    if proposal is None:
        return {}
    # `full` is not consulted: dispatch internals are internal at every
    # authority level. What a submitter needs is whether its work reached
    # a site, not how the delivery went.
    return {
        "dispatched": bool(proposal.dispatched_at),
        "dispatched_at": _iso(proposal.dispatched_at),
        "directive_issued": bool(proposal.directive_id),
    }


def outcome_block(proposal, outcome_row=None) -> dict[str, Any]:
    """The canonical classification, never collapsed into pass/fail.

    `settle` maps five outcome values onto two proposal statuses, so
    `PARTIAL` and `ROLLBACK` both leave the proposal `failed`. A25.4
    forbids letting that collapse be the only thing a machine sees, so
    the classification travels beside the status.
    """
    if proposal is None:
        return {}
    block = {
        "classification": proposal.outcome or "",
        "recorded_at": _iso(proposal.outcome_at),
        "fault_resolved": None,
    }
    if outcome_row is not None:
        block["fault_resolved"] = (
            None if outcome_row.fault_resolved is None
            else bool(outcome_row.fault_resolved)
        )
        block["classification"] = outcome_row.outcome or block["classification"]
    return block


def terminal_block(proposal) -> dict[str, Any]:
    """One boolean, and which layer ended it."""
    status = getattr(proposal, "status", "") or ""
    terminal = is_terminal(status)
    return {
        "terminal": terminal,
        "terminal_layer": TERMINAL_STATES.get(status, ""),
        # Stated rather than left to be inferred: `approved` is a decision,
        # not an ending, and a consumer that stopped polling there would
        # miss dispatch, outcome, and a budget withdrawal returning it to
        # the queue.
        "note": (
            "" if terminal else
            "not terminal; approved means decided, never executed"
        ),
    }


# ---------------------------------------------------------------------------
# The receipt
# ---------------------------------------------------------------------------


async def resolve_governing_policy(
    session: Any, tenant_id: str, action_type: str, device_agent_id: str,
    *, cache: Optional[dict] = None,
):
    """(policy, group) for these coordinates. The ONLY cacheable half.

    Split out because the first version of `machine_proposal_items` cached
    the WHOLE completion under `(action_type, device_agent_id)` -- and a
    completion is read from `cc_approval_records` by `subject_ref =
    proposal.id`, which those two coordinates do not determine. Two
    proposals by one agent for one action class on one device therefore
    shared one answer: whichever the list reached first decided the
    approval state of the other. An approved proposal could report a
    pending sibling as APPROVED, or conceal a real approval behind a
    pending one, purely by list order.

    Policy RESOLUTION is genuinely proposal-independent -- `resolve_policy`
    selects on action class, device type and risk, none of which vary
    between two proposals sharing those coordinates -- so it is the part
    that may be cached, and it is cached here alone.
    """
    from harkeniq_cc.api.approvals import governing_policy

    key = (action_type or "", device_agent_id or "")
    if cache is not None and key in cache:
        return cache[key]
    policy, group, _members = await governing_policy(
        session, tenant_id, action_type, device_agent_id,
    )
    if cache is not None:
        cache[key] = (policy, group)
    return policy, group


async def approval_completion(
    session: Any, tenant_id: str, proposal, *, policy_cache: Optional[dict] = None,
) -> Optional[dict]:
    """Ask the CANONICAL approval system, never a second opinion.

    The first version of this called
    `evaluate_completion(records, len(records))`, deriving "how many are
    needed" from "how many have decided". That is not an approximation,
    it is the wrong question: a policy requiring two approvers with one
    record present would compute needed=1 and report the subject
    APPROVED. A tenant that had configured dual authorization would have
    been told, over a machine contract, that a single approval completed
    it -- E0.1's own defect, arriving at a fifth origin.

    So the governing policy and group are resolved through the same path
    a real decision takes (`governing_policy`), and completion is decided
    by the same rule (`approval_block`). There is one approval system;
    this reads it.

    Returns the canonical block, or None when the subject has no policy
    and no records to speak of. The CALLER projects it -- identities in
    this payload never reach a machine principal (A25.3).
    """
    from harkeniq_cc.approval_policy import (
        SUBJECT_AGENT_PROPOSAL, approval_block as canonical_block,
    )
    from harkeniq_cc.db.repos import ApprovalRecordRepo

    # PER PROPOSAL, ALWAYS. `subject_ref` is the proposal id, so this read
    # is what makes the answer this proposal's own; it is never cached and
    # never shared with a sibling.
    records = await ApprovalRecordRepo(session).list_for_subject(
        SUBJECT_AGENT_PROPOSAL, proposal.id,
    )
    policy, group = await resolve_governing_policy(
        session, tenant_id, proposal.action_type, proposal.device_agent_id,
        cache=policy_cache,
    )
    if not records and policy is None:
        return None
    return canonical_block(policy, group, records)


async def build_receipt(
    session: Any,
    *,
    tenant_id: str,
    submission=None,
    proposal=None,
    authority: bool,
) -> dict[str, Any]:
    """One receipt, from canonical state only.

    `authority` is the caller's CURRENT reach over this work, resolved by
    the route through the one scope resolver. It decides how much of the
    estate the receipt may describe (A25.2) — never whether the receipt
    exists, which the caller's identity already settled.
    """
    from harkeniq_cc.db.repos import OutcomeHistoryRepo

    completion = None
    outcome_row = None
    if proposal is not None:
        completion = await approval_completion(session, tenant_id, proposal)
        if proposal.directive_id:
            outcome_row = await OutcomeHistoryRepo(session).find_by_action_id(
                tenant_id, f"directive:{proposal.directive_id}",
            )

    return {
        "view": VIEW_FULL if authority else VIEW_RECEIPT,
        "authority": (
            "current" if authority else "historical_attribution_only"
        ),
        "submission": submission_block(submission),
        "proposal": proposal_block(proposal, full=authority),
        "approval": approval_block(completion, proposal),
        "execution": execution_block(proposal, full=authority),
        "outcome": outcome_block(proposal, outcome_row if authority else None),
        "terminal": terminal_block(proposal),
        "governs": (
            "Lifecycle state only. This receipt confers nothing and "
            "authorizes nothing."
        ) if authority else (
            "Historical attribution for your own submission. Your current "
            "scope no longer covers this work, so estate detail is "
            "withheld."
        ),
    }


# ---------------------------------------------------------------------------
# The machine-safe list item (A25.3/A25.4, and the leak HIGH 2 found)
# ---------------------------------------------------------------------------


async def machine_proposal_items(
    session: Any, tenant_id: str, proposals, *, authority_for
) -> list[dict[str, Any]]:
    """The proposal list, as a MACHINE principal may see it.

    `proposal_dict` is the Console's payload and is correct there: it
    carries `decided_by`, raw `evidence`, executable `params`,
    `directive_id`, dispatch internals and full estate detail. None of
    that may reach a machine principal (A25.3), and `list_proposals`
    became machine-self-readable in this slice -- so the projection had
    to follow the authorization rather than be left behind by it.

    NO SECOND LIFECYCLE. Every field here comes from the same blocks the
    receipt is built from, so a list item and a receipt for one proposal
    can never describe it differently.

    `authority_for(proposal) -> bool` is supplied by the caller, which
    owns the scope question.

    WHAT IS CACHED, AND WHAT MUST NEVER BE. The POLICY resolution is
    cached per (action_type, device) so a list of fifty proposals over
    three device classes does not become fifty policy resolutions. The
    COMPLETION is not cached and cannot be: it is read from the approval
    ledger by `subject_ref = proposal.id`. Caching it under the policy
    coordinates made two proposals sharing an agent, a device and an
    action class share one approval state -- so a 2-of-2 approved
    proposal and an untouched 0-of-2 sibling reported the same thing,
    decided by list order.
    """
    policy_cache: dict[tuple[str, str], Any] = {}
    items: list[dict[str, Any]] = []
    for proposal in proposals:
        completion = await approval_completion(
            session, tenant_id, proposal, policy_cache=policy_cache,
        )
        full = authority_for(proposal)
        items.append({
            "proposal_id": proposal.id,
            "created_at": _iso(proposal.created_at),
            "proposal": proposal_block(proposal, full=full),
            "approval": approval_block(completion, proposal),
            "execution": execution_block(proposal, full=full),
            "outcome": outcome_block(proposal),
            "terminal": terminal_block(proposal),
        })
    return items


# ---------------------------------------------------------------------------
# The machine-safe AGENT projections (A25.3/A25.5, and the leak HIGH 1 found)
# ---------------------------------------------------------------------------
#
# `get_agent`, `list_agents`, the preflight read, the runtime read and the
# identity read all became reachable by a machine principal. The
# AUTHORIZATION moved (an agent may inspect itself) and the PROJECTION did
# not, so an external runtime was handed the Console's payload:
# `proposal_dict` with executable `params`, raw `evidence`,
# `authorization_basis`, `decided_by`, `directive_id` and
# `dispatch_reason`; `created_by`/`activated_by`/`acknowledged_by` naming
# real people; and the activation approval block naming approvers.
#
# EVERY function below builds its answer by NAMING the fields it may pass.
# None of them takes a rich payload and deletes from it. That distinction
# is the whole control: a projection that filtered by exclusion would leak
# the next field somebody adds upstream, which is exactly how this surface
# came to leak in the first place.

#: Named so a test can assert them, and so a reader can see at a glance
#: what a machine principal is never told on this surface. Not consulted
#: at runtime -- nothing here filters -- but a hostile serialization test
#: sweeps every machine response for these, recursively.
#:
#: WHO decided, built, ran, acknowledged, issued or revoked anything.
#: A25.3 excludes approver identity from every machine surface; the same
#: reasoning covers every other operator whose name appears beside a
#: governance act. There is NO machine response on which one of these may
#: appear, dry-run included.
MACHINE_IDENTITY_FIELDS: frozenset[str] = frozenset({
    "decided_by",            # approver identity (A25.3)
    "approvers",             # approver identities (A25.3)
    "denied_by",             # denier identity (A25.3)
    "denied_reason",         # a human's words to another human
    "created_by",            # the operator who built the agent
    "activated_by",          # the operator who switched it on
    "updated_by",            # the operator who last edited it
    "produced_by",           # the operator who ran the preflight
    "acknowledged_by",       # the operator who accepted the warnings
    "activation_acknowledged_by",
    "issued_by",             # the operator who issued the credential
    "rotated_by",
    "revoked_by",
    "policy_name",           # the tenant's governance configuration
    "policy_id",
    "group_name",
    "group_id",
})

#: Execution and delivery internals. Absent from every LIFECYCLE/STATUS
#: projection -- the receipt, the proposal list, the agent detail, the
#: runtime and preflight reads.
#:
#: Dry-run is the deliberate exception and the only one: A22.2 requires it
#: to return the REAL resolved parameters, evidence and rationale, because
#: that is the field that exposed the A4 defect where every proposal
#: carried `{"reason": ...}` whatever the class. Those are the agent's own
#: reasoning about work it has not proposed, not another party's data --
#: and it still may not learn who decided anything, which is why the set
#: above has no exception.
MACHINE_INTERNAL_FIELDS: frozenset[str] = frozenset({
    "params",                # executable payload; server-derived, not authored
    "evidence",              # raw diagnostic evidence
    "rationale",             # free text composed for a human decision-maker
    "authorization_basis",   # how the platform justified running it
    "directive_id",          # internal Site Manager correlation handle
    "dispatch_reason",       # internal delivery detail
})

MACHINE_WITHHELD_FIELDS: frozenset[str] = (
    MACHINE_IDENTITY_FIELDS | MACHINE_INTERNAL_FIELDS
)


def _machine_action_class(row: dict) -> dict[str, Any]:
    """One bound action class, as its own agent may see it.

    Disposition, reason and the conditions blocking it -- which is what a
    runtime needs to know whether to bother proposing. `evidence`,
    `learning` and `advancement` are the operator's advancement story:
    aggregate outcome statistics and learned signals carrying site
    references, composed for a person deciding whether to raise a level.
    They are not named here.
    """
    return {
        "action_type": row.get("action_type"),
        "known_to_executor": bool(row.get("known_to_executor")),
        "risk": row.get("risk"),
        "granted_at_level": row.get("granted_at_level"),
        "never_budget_grantable": row.get("never_budget_grantable"),
        "tenant_disposition": row.get("tenant_disposition"),
        "disposition": row.get("disposition"),
        "disposition_reason": _bounded(row.get("disposition_reason") or ""),
        "blocking_conditions": row.get("blocking_conditions") or [],
        "requires_approval": bool(row.get("requires_approval", True)),
        "capability": row.get("capability"),
    }


def machine_agent_identity(agent) -> dict[str, Any]:
    """Who this agent is and what state it is in. Never who built it."""
    from harkeniq_cc.agent_activation import activation_provenance
    from harkeniq_cc.operational_agent import attribution_key

    return {
        "id": agent.id,
        "tenant_id": agent.tenant_id,
        "name": agent.name,
        "status": agent.status,
        "version": int(agent.version),
        "actor": attribution_key(agent.id, agent.version),
        "species": "agent",
        **activation_provenance(agent),
        "acknowledgement_current": (
            bool(getattr(agent, "activation_acknowledged_by", ""))
            and int(getattr(agent, "activation_acknowledged_version", 0) or 0)
            == int(agent.version)
        ),
        "autonomy_ceiling": agent.autonomy_ceiling,
        "require_approval_always": bool(agent.require_approval_always),
        "max_proposals_per_day": int(agent.max_proposals_per_day or 0),
        "execution_budget": int(getattr(agent, "execution_budget", 0) or 0),
        "budget_period": getattr(agent, "budget_period", "daily"),
        "paused": bool(getattr(agent, "paused_reason", "")),
        "paused_reason": _bounded(getattr(agent, "paused_reason", "") or ""),
        "last_evaluated_at": _iso(getattr(agent, "last_evaluated_at", None)),
    }


async def machine_agent_view(
    session: Any,
    *,
    tenant_id: str,
    agent,
    human_view: dict,
    proposals=(),
    authority_for,
) -> dict[str, Any]:
    """`GET /{agent_id}` as a machine principal may see it (A25.3/A25.5).

    Composed FROM the same `agent_view` the Console reads, so the machine
    and the operator can never be told different things about a
    disposition -- and then reduced by naming what may pass.

    The estate is deliberately NOT enumerated. An agent learns what it
    can see through the governed fleet surfaces and its own dry-run; this
    read answers "what am I, what may I do, what have I done", and a
    device inventory here would be a second, unmetered way to ask a
    question those surfaces already answer under their own scope rules.
    """
    scope = human_view.get("scope") or {}
    capabilities = human_view.get("capabilities") or {}
    posture = human_view.get("posture") or {}
    return {
        "view": VIEW_MACHINE,
        "contract_version": human_view.get("contract_version"),
        "generated_at": human_view.get("generated_at"),
        "agent": machine_agent_identity(agent),
        "scope": {
            "rules": scope.get("rules") or [],
            "device_count": int(scope.get("device_count") or 0),
            "reads": scope.get("reads") or [],
            "explicit": bool(scope.get("explicit")),
        },
        "capabilities": {
            "action_classes": [
                _machine_action_class(row)
                for row in (capabilities.get("action_classes") or [])
            ],
            "skills": capabilities.get("skills") or [],
            "autonomous_now": capabilities.get("autonomous_now") or [],
            "needs_approval": capabilities.get("needs_approval") or [],
            "denied": capabilities.get("denied") or [],
        },
        "activity": human_view.get("activity") or {},
        "posture": {
            "tenant_level": posture.get("tenant_level"),
            "stop_switch": bool((posture.get("stop_switch") or {}).get("active")),
            "safety_reported": bool(posture.get("safety_reported")),
            # A COUNT. Which sites are silent is estate detail; that this
            # agent may be acting on stale safety is not.
            "sites_not_reporting": len(posture.get("sites_not_reporting") or []),
        },
        "proposals": await machine_proposal_items(
            session, tenant_id, proposals, authority_for=authority_for,
        ),
        "governs": (
            "Configuration and lifecycle state for your own agent. This "
            "view confers nothing and authorizes nothing."
        ),
    }


def machine_agent_list_item(agent, scopes=(), proposals=()) -> dict[str, Any]:
    """One row of `GET /`, for a machine reading its own agent."""
    by_status: dict[str, int] = {}
    for proposal in proposals:
        by_status[proposal.status] = by_status.get(proposal.status, 0) + 1
    return {
        **machine_agent_identity(agent),
        "scopes": [
            {"scope_type": s.scope_type, "scope_ref": s.scope_ref} for s in scopes
        ],
        "proposal_counts": by_status,
    }


def machine_preflight_view(payload: dict) -> dict[str, Any]:
    """The activation readiness contract, without the people in it.

    `produced_by` and `acknowledged_by` name operators, and
    `activation_approval` embeds the canonical completion block, which
    carries `approvers` (with email addresses), `denied_by`,
    `denied_reason` and the governing policy's name. A25.3 forbids every
    one of those reaching a machine, so this names the six facts that may
    pass: whether approval is required, its state, and the counts.
    """
    if not payload.get("exists", True):
        return {
            "view": VIEW_MACHINE,
            "agent_id": payload.get("agent_id"),
            "exists": False,
            "configuration_version": payload.get("configuration_version"),
            "detail": _bounded(payload.get("detail") or ""),
        }
    approval = payload.get("activation_approval") or None
    return {
        "view": VIEW_MACHINE,
        "agent_id": payload.get("agent_id"),
        "exists": True,
        "current": bool(payload.get("current")),
        "produced_at": payload.get("produced_at"),
        "acknowledgement_current": bool(payload.get("acknowledgement_current")),
        "acknowledged": bool(payload.get("acknowledged_by")),
        "configuration_version": payload.get("configuration_version"),
        "overall": payload.get("overall"),
        "can_activate": bool(payload.get("can_activate")),
        "requires_acknowledgement": bool(payload.get("requires_acknowledgement")),
        "requires_activation_approval": bool(
            payload.get("requires_activation_approval")
        ),
        "unattended_classes": payload.get("unattended_classes") or [],
        "blocked_dimensions": payload.get("blocked_dimensions") or [],
        "warn_dimensions": payload.get("warn_dimensions") or [],
        "unknown_dimensions": payload.get("unknown_dimensions") or [],
        "by_dimension": payload.get("by_dimension") or {},
        # `dimension`, not `name`: the key the human contract already uses,
        # so the two views name the same thing. The `**extra` each row may
        # carry (counts, class lists, and whatever a later dimension adds)
        # is NOT passed through -- an allow-list that forwarded an open
        # dict would be the subtractive filter this module exists to avoid.
        "dimensions": [
            {
                "dimension": d.get("dimension"),
                "verdict": d.get("verdict"),
                "detail": _bounded(d.get("detail") or ""),
            }
            for d in (payload.get("dimensions") or [])
        ],
        "activation_approval": None if approval is None else {
            "required": True,
            "state": approval.get("state", "pending"),
            "granted_count": int(approval.get("received", 0)),
            "required_count": int(approval.get("required", 0)),
        },
        "contract": payload.get("contract") or {},
    }


def machine_runtime_view(payload: dict) -> dict[str, Any]:
    """The runtime health read, named field by field."""
    return {
        "view": VIEW_MACHINE,
        "agent_id": payload.get("agent_id"),
        "actor": payload.get("actor"),
        "activation_state": payload.get("activation_state"),
        "configuration_version": payload.get("configuration_version"),
        "activated_version": payload.get("activated_version"),
        "activation_provenance": payload.get("activation_provenance"),
        "configuration_drifted": bool(payload.get("configuration_drifted")),
        "last_evaluated_at": payload.get("last_evaluated_at"),
        "evaluation": payload.get("evaluation"),
        "devices": payload.get("devices") or {},
        "budget": payload.get("budget") or {},
        "proposals_in_window": int(payload.get("proposals_in_window") or 0),
        # Counts by state. `skills_by_id` enumerates every device a skill
        # reached or skipped, with the reason -- an operator's report, and
        # a device inventory this surface does not hand out.
        "skills": payload.get("skills") or {},
        "paused_reason": _bounded(payload.get("paused_reason") or ""),
        "preflight": payload.get("preflight") or {},
    }


def machine_identity_view(payload: dict) -> dict[str, Any]:
    """Its own credential's state. Never who issued, rotated or revoked it."""
    if not payload.get("exists", True):
        return {
            "view": VIEW_MACHINE,
            "agent_id": payload.get("agent_id"),
            "exists": False,
            "detail": _bounded(payload.get("detail") or ""),
        }
    return {
        "view": VIEW_MACHINE,
        "exists": True,
        "agent_id": payload.get("agent_id"),
        "client_id": payload.get("client_id"),
        "realm": payload.get("realm"),
        "status": payload.get("status"),
        "issued_at": payload.get("issued_at"),
        "rotated_at": payload.get("rotated_at"),
        "revoked_at": payload.get("revoked_at"),
        "last_seen_at": payload.get("last_seen_at"),
        "last_seen_source": payload.get("last_seen_source"),
        "contract": payload.get("contract"),
    }
