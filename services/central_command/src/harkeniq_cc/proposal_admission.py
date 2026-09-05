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
from typing import Any, Optional

logger = logging.getLogger("harkeniq.cc.proposal_admission")

#: The advisory-lock namespace for a tenant's proposal admission.
#: Per TENANT, not per key: `all_dedupe_keys` reads the whole tenant, so
#: the read this lock protects is tenant-wide and a per-key lock would
#: not actually serialize it.
_ADMISSION_LOCK = "cc.proposal_admission.{tenant_id}"

#: How a proposal came to exist. Provenance for the audit trail and the
#: Console, never an authorization input: an externally submitted
#: proposal is governed identically to an internally derived one.
ORIGIN_EVALUATOR = "evaluator"
ORIGIN_INGRESS = "ingress"

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

    `origin` records HOW the proposal came to exist -- `evaluator` or
    `ingress`. It is provenance for the audit trail and the Console, and
    is deliberately NOT an authorization input: an externally submitted
    proposal is governed identically to an internally derived one.

    Returns ``(row_or_None, code, reason)``.
    """
    from harkeniq.capabilities import operation_key
    from harkeniq_cc.db.repos import AgentProposalRepo, AuditRepo

    await lock_proposal_admission(session, tenant_id)

    repo = AgentProposalRepo(session)
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

    row = await repo.create(**payload)

    detail = {
        "agent_id": payload.get("agent_id", ""),
        "action_type": row.action_type,
        "device_agent_id": row.device_agent_id,
        "disposition": row.disposition,
        "status": row.status,
        "reason": (row.disposition_reason or "")[:200],
        # A24: provenance travels with the record from the first moment,
        # so an approver is never left guessing whether HarkenIQ's own
        # loop proposed this or an external runtime asked for it.
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
