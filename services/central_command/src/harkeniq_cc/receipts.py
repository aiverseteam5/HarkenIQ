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


async def approval_completion(session: Any, tenant_id: str, proposal) -> Optional[dict]:
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
    from harkeniq_cc.api.approvals import governing_policy
    from harkeniq_cc.approval_policy import (
        SUBJECT_AGENT_PROPOSAL, approval_block as canonical_block,
    )
    from harkeniq_cc.db.repos import ApprovalRecordRepo

    records = await ApprovalRecordRepo(session).list_for_subject(
        SUBJECT_AGENT_PROPOSAL, proposal.id,
    )
    policy, group, _members = await governing_policy(
        session, tenant_id, proposal.action_type, proposal.device_agent_id,
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
    owns the scope question; the policy resolution is cached per
    (action_type, device) because a list of fifty proposals over three
    device classes must not become fifty policy resolutions.
    """
    cache: dict[tuple[str, str], Optional[dict]] = {}
    items: list[dict[str, Any]] = []
    for proposal in proposals:
        key = (proposal.action_type or "", proposal.device_agent_id or "")
        if key not in cache:
            cache[key] = await approval_completion(session, tenant_id, proposal)
        completion = cache[key]
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
