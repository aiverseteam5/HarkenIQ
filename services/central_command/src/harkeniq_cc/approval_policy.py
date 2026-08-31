"""Approval policy: which policy governs a decision, and when it is complete.

E0.1 (2026-08-30). `cc_approval_policies` has existed since R2b carrying
`approval_mode`, `required_approvers` and a group link. The Console has
full CRUD for it and the S5 autonomy contract faithfully reports it. Until
now **nothing consulted it when a decision was made**: a tenant could
configure dual authorization and get single authorization, silently.

This module is the judgement half of the fix. It is pure -- no I/O, no
database, no clock of its own -- so the rules can be tested without a
stack and cannot differ between the human path and the agent path,
because there is only one implementation of them.

Two axes, deliberately separate
-------------------------------
  WHICH POLICY governs this action class     `resolve_policy`
  IS THE DECISION COMPLETE under it          `evaluate_completion`

Why `auto_approve` is refused
-----------------------------
The Console ships a policy preset with `approval_mode="auto_approve"`.
While nothing enforced policies it was inert. Enforcing it as written
would make a single policy row a second, ungoverned path to unattended
execution: no evidence bar, no budget, no error-budget drop-back, and --
worst -- no fence for the risk-`high` classes that
`never_budget_grantable` refuses at EVERY autonomy level.

The tenant's autonomy contract is the one governed answer to "may this
run without a human". So this module treats `auto_approve` as
`require_approval` and the policy API refuses to store it. That is a
refusal to create a bypass, not a change to the autonomy model.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

#: Bump when a consumer would have to change to read the payload.
CONTRACT_VERSION = "1"

MODE_REQUIRE_APPROVAL = "require_approval"
#: Refused on write, coerced on read. See the module docstring.
MODE_AUTO_APPROVE = "auto_approve"

SUBJECT_ACTION = "action"
SUBJECT_AGENT_PROPOSAL = "agent_proposal"
#: S6: one site-wave of a campaign. A THIRD origin on the same
#: ledger, deliberately not a third approval model -- the decision
#: function, the policy resolution, the required-approver count and
#: the duplicate guarantee are all the ones a node action already
#: gets. Approval granularity is per site-wave for every action that
#: requires a human (D1): batching several waves is a review
#: affordance in the Console, never a merged decision record.
SUBJECT_CAMPAIGN_WAVE = "campaign_wave"
#: A2: activating an Operational Agent whose configuration would
#: grant real UNATTENDED execution (D1). A fourth origin on the
#: same ledger, not a fourth approval model -- the policy
#: resolution, approver count, group rule and duplicate guarantee
#: are the ones a node action already gets. Activation approval
#: grants no RBAC, scope or capability authority: it approves a
#: configuration the actor was already permitted to build.
SUBJECT_AGENT_ACTIVATION = "agent_activation"

DECISION_APPROVED = "approved"
DECISION_DENIED = "denied"

STATE_PENDING = "pending"
STATE_APPROVED = "approved"
STATE_DENIED = "denied"

WILDCARD = "*"


def effective_mode(policy: Any) -> str:
    """The mode actually enforced. Never returns `auto_approve`."""
    mode = (getattr(policy, "approval_mode", "") or MODE_REQUIRE_APPROVAL).lower()
    if mode == MODE_AUTO_APPROVE:
        return MODE_REQUIRE_APPROVAL
    return mode or MODE_REQUIRE_APPROVAL


def _specificity(policy: Any, action_type: str, device_type: str, risk: str) -> Optional[int]:
    """How precisely this policy matches, or None when it does not.

    Higher is more specific. Action type outweighs device type, which
    outweighs risk, so a rule written for one action class always beats a
    broader rule that happens to share its risk band.
    """
    if (getattr(policy, "status", "active") or "active") != "active":
        return None
    score = 0
    for field, want, weight in (
        ("action_type", action_type, 4),
        ("device_type", device_type, 2),
        ("risk_level", risk, 1),
    ):
        declared = (getattr(policy, field, WILDCARD) or WILDCARD)
        if declared == WILDCARD:
            continue
        if declared.lower() != (want or "").lower():
            return None
        score += weight
    return score


def resolve_policy(
    policies: Iterable[Any],
    *,
    action_type: str,
    device_type: str = "",
    risk: str = "",
) -> Optional[Any]:
    """The one policy governing this action, or None.

    Most specific wins; ties break deterministically on id so two runs
    over the same configuration never choose differently. None means no
    policy is configured, which is not the same as "no approval needed":
    the caller falls back to a single approver, the behaviour every
    tenant has today.
    """
    best: Optional[Any] = None
    best_key: Optional[tuple] = None
    for policy in policies:
        score = _specificity(policy, action_type, device_type, risk)
        if score is None:
            continue
        key = (score, str(getattr(policy, "id", "")))
        if best_key is None or key > best_key:
            best, best_key = policy, key
    return best


def required_approvers(policy: Any, group: Any = None) -> int:
    """How many distinct approvals this subject needs.

    The policy's count wins. A bound group's `required_count` applies
    only when the policy left its own count at the default, so binding a
    group can raise the bar but never quietly lower a number an operator
    typed.
    """
    if policy is None:
        return 1
    count = int(getattr(policy, "required_approvers", 1) or 1)
    if count > 1:
        return count
    if group is not None:
        return max(1, int(getattr(group, "required_count", 1) or 1))
    return max(1, count)


def is_member(group_members: Iterable[Any], approver_ref: str, approver_email: str) -> bool:
    """Whether this approver belongs to the bound group.

    Matches the Keycloak subject first and falls back to the email,
    because membership rows predate `principal_ref` and an address change
    must not silently lapse someone's authority.
    """
    ref = (approver_ref or "").strip()
    email = (approver_email or "").strip().lower()
    for member in group_members:
        if ref and (getattr(member, "principal_ref", "") or "") == ref:
            return True
        if email and (getattr(member, "user_email", "") or "").strip().lower() == email:
            return True
    return False


def evaluate_completion(records: Iterable[Any], needed: int) -> dict[str, Any]:
    """Is this subject decided, and if not, what is it waiting for.

    A single denial is terminal (D16: denied is final) and outranks any
    number of approvals, so an approver who objects cannot be outvoted by
    colleagues clicking faster. Only approvals whose scope covered the
    subject are counted.
    """
    records = list(records)
    denials = [r for r in records if r.decision == DECISION_DENIED]
    approvals = [
        r for r in records
        if r.decision == DECISION_APPROVED and getattr(r, "scope_ok", True)
    ]
    # Distinct approvers, in case a caller ever writes without the
    # uniqueness constraint (a different database, a future bulk import).
    seen: set[str] = set()
    distinct = []
    for record in approvals:
        if record.approver_ref in seen:
            continue
        seen.add(record.approver_ref)
        distinct.append(record)

    if denials:
        state = STATE_DENIED
    elif len(distinct) >= max(1, needed):
        state = STATE_APPROVED
    else:
        state = STATE_PENDING

    return {
        "state": state,
        "required": max(1, needed),
        "received": len(distinct),
        "remaining": max(0, max(1, needed) - len(distinct)),
        "approvers": [
            {
                "approver": r.approver_email or r.approver_ref,
                "decided_at": r.decided_at.isoformat() if r.decided_at else None,
            }
            for r in distinct
        ],
        "denied_by": (
            denials[0].approver_email or denials[0].approver_ref if denials else None
        ),
        "denied_reason": denials[0].reason if denials else "",
    }


def approval_block(
    policy: Any, group: Any, records: Iterable[Any]
) -> dict[str, Any]:
    """The progress payload a queue item carries. One shape, both origins."""
    needed = required_approvers(policy, group)
    block = evaluate_completion(records, needed)
    block.update({
        "contract_version": CONTRACT_VERSION,
        "policy_id": getattr(policy, "id", None),
        "policy_name": getattr(policy, "name", "") or "",
        "mode": effective_mode(policy) if policy is not None else MODE_REQUIRE_APPROVAL,
        "group_id": getattr(group, "id", None),
        "group_name": getattr(group, "name", "") or "",
    })
    return block
