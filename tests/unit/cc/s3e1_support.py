"""S3-E1 (A30.37) test support: the gate verdicts and site reports tests need.

`build_autonomy` takes the global safety verdicts as a REQUIRED input -- there
is no default that could mean "no gate" -- so a test that composes a contract
directly has to say which gate it means. `clear_gate()` is exactly what the
production registry (empty, D1) evaluates to: every class CLEAR.

A site that has not reported recently is NOT REPORTED and requires approval
(R3(e), D8), so a fixture that expects autonomy has to have its sites speak.
`fresh_report()` is one site's current, reported safety state.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Optional

from harkeniq_cc.global_safety import GlobalSafetyGate, estate_from_rows
from harkeniq_cc.governance import gate_verdicts


def clear_gate(target_site_id: str = "") -> dict:
    """{action_type: CLEAR} -- what the EMPTY production registry evaluates to."""
    return gate_verdicts(
        GlobalSafetyGate(members=(), tenant_id="t", estate=()),
        target_site_id=target_site_id,
    )


def gate_with(
    *members, safety_rows=(), target_site_id: str = "", tenant_id: str = "t",
    now: Optional[datetime] = None,
) -> dict:
    """{action_type: verdict} for a gate holding `members` over `safety_rows`."""
    now = now or datetime.now(timezone.utc)
    return gate_verdicts(
        GlobalSafetyGate(
            members=members, tenant_id=tenant_id,
            estate=estate_from_rows(safety_rows, now), now=now,
        ),
        target_site_id=target_site_id,
    )


def fresh_report(site_id: str, *, now: Optional[datetime] = None, **fields):
    """One site's CURRENT, reported safety state (a `cc_safety_state` row)."""
    now = now or datetime.now(timezone.utc)
    row = dict(
        site_id=site_id, reported=True, as_of=now, ingested_at=now,
        sm_stop_switch=False, suppressions=[], error_budgets=[],
        site_budgets={},
    )
    row.update(fields)
    return SimpleNamespace(**row)


async def record_activation(
    session, *, tenant_id: str, agent, unattended, approver_ref: str = "kc-activator",
    approved: bool = True, at: Optional[datetime] = None,
) -> str:
    """What a REAL activation leaves behind, for D11 to recover (A30.37).

    The agent active at a recorded version and time; the immutable preflight
    row that was current at that moment, carrying its unattended set; and,
    when that set is non-empty, an approval of exactly that subject on the
    E0.1 ledger. Returns the activation subject ("" when none was needed).
    """
    from datetime import timedelta

    from harkeniq_cc.agent_activation import activation_subject_ref
    from harkeniq_cc.approval_policy import SUBJECT_AGENT_ACTIVATION
    from harkeniq_cc.db.models import CCAgentPreflight, CCApprovalRecord

    at = at or datetime.now(timezone.utc)
    unattended = sorted(str(c).upper() for c in unattended)
    agent.status = "active"
    agent.activated_version = int(agent.version)
    agent.activated_at = at
    session.add(CCAgentPreflight(
        agent_id=agent.id, tenant_id=tenant_id,
        configuration_version=int(agent.version), overall="warn",
        can_activate=True, requires_acknowledgement=False,
        requires_activation_approval=bool(unattended),
        result={"unattended_classes": unattended},
        produced_by="builder@example.com",
        produced_at=at - timedelta(seconds=2),
    ))
    subject = ""
    if unattended:
        subject = activation_subject_ref(agent.id, int(agent.version), unattended)
        agent.activation_subject_ref = subject
        session.add(CCApprovalRecord(
            tenant_id=tenant_id, subject_type=SUBJECT_AGENT_ACTIVATION,
            subject_ref=subject, approver_ref=approver_ref,
            approver_email=f"{approver_ref}@example.com",
            decision="approved" if approved else "denied",
            decided_at=at - timedelta(seconds=1),
        ))
    await session.flush()
    return subject
