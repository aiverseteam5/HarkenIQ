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
        # Estate detail. A25.2 forbids every line of this once current
        # authority is gone -- which is why it is added here rather than
        # stripped somewhere else.
        block.update({
            "action_type": proposal.action_type,
            "device_agent_id": proposal.device_agent_id,
            "site_id": proposal.site_id,
            "params": proposal.params or {},
            "blocking_conditions": proposal.blocking_conditions or [],
            "authorization_basis": proposal.authorization_basis,
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


def execution_block(proposal, *, full: bool) -> dict[str, Any]:
    """Whether it reached a site — not the internal handle that took it.

    `directive_id` is Central Command's link to a Site Manager record and
    is of no use to the submitter; D3 keeps internal correlation handles
    out of the external contract, so this reports only that a directive
    exists.
    """
    if proposal is None:
        return {}
    block = {
        "dispatched": bool(proposal.dispatched_at),
        "dispatched_at": _iso(proposal.dispatched_at),
        "directive_issued": bool(proposal.directive_id),
    }
    if full:
        block["dispatch_reason"] = _bounded(proposal.dispatch_reason)
    return block


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
    from harkeniq_cc.approval_policy import SUBJECT_AGENT_PROPOSAL
    from harkeniq_cc.db.repos import ApprovalRecordRepo, OutcomeHistoryRepo

    completion = None
    outcome_row = None
    if proposal is not None:
        records = await ApprovalRecordRepo(session).list_for_subject(
            SUBJECT_AGENT_PROPOSAL, proposal.id,
        )
        if records:
            from harkeniq_cc.approval_policy import evaluate_completion

            # `required` here is the count actually recorded against the
            # subject. The policy that set it is not machine-visible.
            completion = evaluate_completion(records, len(records))
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
