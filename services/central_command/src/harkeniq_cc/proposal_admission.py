"""A24.6: the ONE path by which a proposal comes into existence.

Before A6 there was exactly one caller that created a proposal -- the
CC-resident evaluator, inline in `agent_runtime.evaluate_agents` -- so
"one admission path" was true by accident rather than by construction.
External ingress is the second caller, and two callers writing the same
row two ways is how governance models drift apart.

WHAT THIS MODULE GUARANTEES
---------------------------
Two DIFFERENT duplicate guarantees are needed, and neither substitutes
for the other:

* **Transport replay** -- the same submission arriving twice with the
  same idempotency key. That is answered by a unique constraint on
  `cc_agent_submissions`, not here.
* **Logical duplication** -- the same governed candidate admitted twice,
  concurrently, by two callers presenting DIFFERENT idempotency keys.
  The replay key cannot see that: the keys differ, so nothing collides.
  That is what this module answers.

WHY A LOCK AND NOT A CONSTRAINT
-------------------------------
The obvious answer -- `unique(tenant_id, dedupe_key)` -- encodes a rule
the platform does not actually use, and inspecting the lifecycle is what
settled it.

`AgentProposalRepo.all_dedupe_keys()` returns EVERY key, not only the
open ones, and its docstring records why: a permanently-refused
`SEL_CLEAR` was re-proposed on every pass until that was fixed. So
admission semantics are already "one proposal ever per dedupe key", and a
partial-unique index over open statuses would describe something else.

A retroactive unique constraint was considered and rejected on three
counts: `dedupe_key` defaults to `""` (many rows would collide on the
empty string), the key shape changed at A5 when the component was
appended, and a migration that fails on historical rows in order to
surface a historical bug is the wrong trade against a customer's upgrade.

So: a transaction-scoped advisory lock around admission, reusing R5-2's
`pg_advisory_chain_lock` exactly as A23-3's `lock_tenant_authorization`
does. Held from the dedupe read through the insert and the caller's
commit, so a second admitter waits, re-reads a COMMITTED key set, and is
refused by the same `duplicate` verdict the evaluator would give it.
No-op on sqlite, which is single-writer anyway; proven concurrently on
real PostgreSQL at the gate.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("harkeniq.cc.proposal_admission")

#: The advisory-lock namespace for a tenant's proposal admission.
#: Per TENANT, not per key: `all_dedupe_keys` reads the whole tenant, so
#: the read this lock protects is tenant-wide and a per-key lock would
#: not actually serialize it.
_ADMISSION_LOCK = "cc.proposal_admission.{tenant_id}"

#: A27.2: PROPOSAL PROVENANCE -- who caused this proposal to exist. A
#: first-class persisted fact from A6-3, and never an authorization
#: input: an externally submitted proposal is governed identically to an
#: internally derived one.
#:
#: DELIBERATELY NOT the approvals `origin`, which answers a different
#: question -- which QUEUE LANE a subject belongs to (`node`, `agent`,
#: `agent_activation`, `campaign_wave`). Overloading one word for two
#: questions is exactly how the approvals surface came to say "agent"
#: for both a proposal HarkenIQ reasoned itself and one an external
#: runtime asked for.
#:
#: A27.3: `external_ingress` is the canonical spelling and is what both
#: the column and NEW audit entries carry. Historical audit entries
#: carry the pre-A27 `"ingress"` and are never rewritten -- the chain is
#: immutable, and rewriting it would be a worse lie than an old spelling.
#:
#: A27.2: the vocabulary is closed and designed to be EXTENDED without
#: reinterpreting today's values. A later ratified source (skill, event,
#: MCP, operator, campaign) becomes a new member; none is implemented.
ORIGIN_EVALUATOR = "evaluator"
ORIGIN_INGRESS = "external_ingress"

#: A27.4: no authoritative provenance exists for this row. Pre-A6-3
#: proposals project as this and are NEVER manufactured into
#: `evaluator` -- unknown is the honest answer, not a default (A17.4).
PROVENANCE_UNKNOWN = "unknown"

#: The closed vocabulary, named so a test can assert it rather than
#: trusting that every producer spells it the same way.
PROVENANCE_TYPES: frozenset[str] = frozenset({
    ORIGIN_EVALUATOR, ORIGIN_INGRESS, PROVENANCE_UNKNOWN,
})

#: The refusal code a logical duplicate is reported under. Deliberately
#: the SAME code `govern_proposal` uses, because it is the same finding:
#: an equivalent proposal is already open. A second vocabulary here would
#: mean the ingress and the evaluator described one situation two ways.
CODE_DUPLICATE = "duplicate"
REASON_DUPLICATE = "an equivalent proposal is already open"

#: A24.12. Distinct from `duplicate` on purpose: "you already asked for
#: this" and "somebody else is already doing this" are different facts,
#: and an operator debugging why an agent went quiet needs to tell them
#: apart.
CODE_OPERATION_IN_FLIGHT = "operation_in_flight"
REASON_OPERATION_IN_FLIGHT = (
    "another agent already has an open proposal for this exact operation "
    "on this device"
)


#: PB-1 (A30.42 D-PB2, A30.44): the agent's admission budget
#: (`max_proposals_per_day`) is spent for today. Recorded on an ingress
#: submission as an attempt, never chained (A24.16), and listed by the
#: dry-run with the same code when the evaluator's own cut is reached.
CODE_PROPOSAL_BUDGET_EXHAUSTED = "proposal_budget_exhausted"
REASON_PROPOSAL_BUDGET_EXHAUSTED = (
    "this agent's proposal budget for today is spent; nothing new is put in "
    "front of governance until the next UTC day"
)

#: The proposing agent no longer exists. Reachable only by a caller racing
#: a deletion: both callers load the agent before they govern anything.
CODE_AGENT_MISSING = "agent_missing"


def admission_day_start(now: Optional[datetime] = None) -> datetime:
    """The start of the admission budget's window: the current UTC day.

    ONE definition, so the admission check, the evaluator's soft cut, the
    ingress re-derivation, the dry-run, discovery and `/runtime` cannot
    disagree about which day a proposal was charged to.
    """
    now = now or datetime.now(timezone.utc)
    return now.astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )


async def admitted_today(
    session: Any, tenant_id: str, agent_id: str, *, now: Optional[datetime] = None,
) -> int:
    """How much of this agent's admission budget today has spent.

    The number `admit_proposal` enforces, read the same way everywhere
    else: rows that are not `blocked`, since the UTC day began, across the
    agent's configuration versions (D-PB2).
    """
    from harkeniq_cc.db.repos import AgentProposalRepo

    return await AgentProposalRepo(session).count_admitted_since(
        tenant_id, agent_id, admission_day_start(now),
    )


def charges_admission(payload: dict) -> bool:
    """Whether admitting this payload spends the admission budget.

    A `blocked` row records a final denial and puts nothing in front of
    governance, so it charges nothing (D-PB2).
    """
    return (payload.get("status") or "") != "blocked"


async def lock_proposal_admission(session: Any, tenant_id: str) -> bool:
    """Serialize this tenant's proposal admission for the rest of this
    transaction.

    Returns True when a real lock was taken (PostgreSQL), False on
    dialects where it is a no-op. Callers must not branch on the return
    value for correctness -- it exists so tests can assert the lock was
    actually reached on a real engine.
    """
    from harkeniq.audit.chain import pg_advisory_chain_lock

    return await pg_advisory_chain_lock(
        session, _ADMISSION_LOCK.format(tenant_id=tenant_id)
    )


async def admit_proposal(
    session: Any,
    *,
    tenant_id: str,
    payload: dict,
    origin: str,
    actor_ref: str = "",
    note: str = "",
) -> tuple[Optional[Any], str, str]:
    """Admit one governed proposal, or refuse it as a logical duplicate.

    `payload` is a `govern_proposal()` proposal dict, unmodified. This
    function does not govern anything and must never start: every
    authorization, capability, parameter and disposition question was
    already answered by the verdict function, and re-deciding any of it
    here would be the second governance path A24.6 forbids.

    What it does own is the moment of creation: take the lock, re-read
    the committed dedupe keys, and refuse if the candidate was admitted
    while this caller was deciding.

    `origin` records HOW the proposal came to exist -- one of
    `PROVENANCE_TYPES`, today `evaluator` or `external_ingress`. It is
    provenance for the audit trail, the column and the Console, and is
    deliberately NOT an authorization input: an externally submitted
    proposal is governed identically to an internally derived one.

    It also owns the admission budget (PB-1, A30.42 D-PB2): a row that
    would be charged is refused once the agent's `max_proposals_per_day`
    is spent, with `proposal_budget_exhausted`. This is the budget's only
    authority; the evaluator's cut is a selection, not a check.

    Returns ``(row_or_None, code, reason)``.
    """
    from harkeniq.capabilities import operation_key
    from harkeniq_cc.db.repos import (
        AgentProposalRepo,
        AuditRepo,
        OperationalAgentRepo,
    )

    await lock_proposal_admission(session, tenant_id)

    repo = AgentProposalRepo(session)

    # PB-1 (A30.42 D-PB2): the admission budget is checked and charged
    # HERE, after the lock, for both callers. The count starts after any
    # earlier admitter committed (READ COMMITTED) and includes this
    # transaction's own earlier inserts, so no path checks and then
    # decrements: lock -> count -> check -> insert -> the caller commits.
    agent_id = payload.get("agent_id", "") or ""
    agent = await OperationalAgentRepo(session).get(tenant_id, agent_id)
    if agent is None:
        return None, CODE_AGENT_MISSING, "the proposing agent no longer exists"
    if charges_admission(payload):
        limit = int(getattr(agent, "max_proposals_per_day", 0) or 0)
        if await admitted_today(session, tenant_id, agent_id) >= limit:
            return (
                None,
                CODE_PROPOSAL_BUDGET_EXHAUSTED,
                REASON_PROPOSAL_BUDGET_EXHAUSTED,
            )

    dedupe_key = payload.get("dedupe_key") or ""
    if dedupe_key and dedupe_key in await repo.all_dedupe_keys(tenant_id):
        return None, CODE_DUPLICATE, REASON_DUPLICATE

    # A24.12: the SAME physical operation, whoever proposed it. The dedupe
    # key above begins with the agent id, so it answers only "has THIS
    # agent already asked" -- two agents could otherwise hold two
    # simultaneously active proposals to do one thing to one device.
    #
    # OPEN proposals only. A settled one must not fence a device forever,
    # and re-running an action that already completed is legitimate work.
    payload = dict(payload)
    payload["operation_key"] = operation_key(
        tenant_id,
        payload.get("device_agent_id", "") or "",
        payload.get("action_type", "") or "",
        payload.get("params") or {},
    )
    conflict = await repo.find_open_operation(tenant_id, payload["operation_key"])
    if conflict is not None:
        return None, CODE_OPERATION_IN_FLIGHT, REASON_OPERATION_IN_FLIGHT

    # A27.5: provenance is written WITH the proposal, in this
    # transaction, from the `origin` this function already receives --
    # so a proposal cannot exist without the provenance of its own
    # creation. No second write path and no inference from the audit log.
    payload["provenance_type"] = origin
    row = await repo.create(**payload)

    detail = {
        "agent_id": payload.get("agent_id", ""),
        "action_type": row.action_type,
        "device_agent_id": row.device_agent_id,
        "disposition": row.disposition,
        "status": row.status,
        "reason": (row.disposition_reason or "")[:200],
        # A24/A27.3: provenance travels with the record from the first
        # moment. It is now ALSO a column (`provenance_type`), which is
        # what makes the claim true where an approver actually reads --
        # the audit entry was the only carrier until A6-3.
        "origin": origin,
    }
    if note:
        detail["note"] = note[:512]

    await AuditRepo(session).append(
        actor=row.actor,
        actor_ref=actor_ref or payload.get("agent_id", "") or None,
        action="agent_proposal.created",
        subject=row.id,
        tenant_id=tenant_id,
        detail=detail,
    )
    logger.info(
        "Proposal %s (%s): %s %s on %s -> %s (%s)",
        row.id, origin, row.actor, row.action_type,
        row.device_agent_id, row.status, row.disposition,
    )
    return row, "", ""
