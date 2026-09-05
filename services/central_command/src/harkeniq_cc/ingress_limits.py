"""A24.11/A24.13: serialization and attempt metering for external ingress.

Two controls that look separate and are not: both are per (tenant, agent),
both must be atomic across replicas, and both sit in front of the same
request. One advisory lock therefore covers both.

WHY A LOCK AND NOT JUST THE UNIQUE CONSTRAINT
---------------------------------------------
The constraint on `cc_agent_submissions` makes a duplicate impossible. It
does not make a *concurrent* duplicate handled. Reproduced on real
PostgreSQL by forcing the route's actual window -- both callers completing
the replay lookup before either inserts -- the loser raises
`UniqueViolationError`, which is a 500 on a retry: exactly the case an
idempotency key exists to make safe.

Worth recording that the first, unforced reproduction did NOT fail: the
two requests happened to serialize, so a green result proved nothing. The
window has to be forced to observe it, which is why this is a lock and not
a hope.

WHY THE LOCK IS PER AGENT AND NOT PER KEY
-----------------------------------------
A per-key lock would serialize only replays of one key, leaving the rate
count -- which is per agent -- still racing. One lock per (tenant, agent)
covers both, and an agent is one logical runtime, so serializing its own
ingress is what an abuse control wants anyway. It also removes a
lock-ordering question: the only other lock in this path is the tenant
admission lock taken inside `admit_proposal`, and the order is always
agent then tenant, so no cycle can form.

WHY ATTEMPTS ARE NOT SUBMISSIONS
--------------------------------
`cc_agent_submissions` is keyed by idempotency key and structurally cannot
hold repeats, so it cannot meter a caller who retries -- and the first
implementation returned a replay BEFORE the rate check, making replay an
unmetered channel. Attempts are their own append-only record, and every
outcome counts: accepted, replayed, conflicting, rejected, refused.

WRITES ARE BOUNDED BY THE LIMIT THEY ENFORCE. Once an agent is over its
window the request is refused WITHOUT recording, so the table cannot be
grown by the traffic it exists to bound.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

#: The advisory-lock namespace for one agent's ingress.
_INGRESS_LOCK = "cc.agent_ingress.{tenant_id}.{agent_id}"

#: The attempt window. Deliberately generous: this is an abuse control,
#: not a product limit. The honest per-agent work cap is
#: `max_proposals_per_day`, which governs CREATION, and the governed
#: refusals do the rest.
ATTEMPT_WINDOW_S = 3600
ATTEMPT_MAX = 240

#: Attempt outcomes. Every one of these counts.
OUTCOME_ACCEPTED = "accepted"
OUTCOME_REPLAYED = "replayed"
OUTCOME_CONFLICT = "conflict"
OUTCOME_REJECTED = "rejected"
OUTCOME_REFUSED = "refused"


async def lock_agent_ingress(session: Any, tenant_id: str, agent_id: str) -> bool:
    """Serialize this agent's ingress for the rest of this transaction.

    Returns True when a real lock was taken (PostgreSQL), False where it is
    a no-op. Callers must not branch on the value for correctness -- it
    exists so a test can assert the lock was reached on a real engine.
    """
    from harkeniq.audit.chain import pg_advisory_chain_lock

    return await pg_advisory_chain_lock(
        session, _INGRESS_LOCK.format(tenant_id=tenant_id, agent_id=agent_id)
    )


async def admit_attempt(
    session: Any, *, tenant_id: str, agent_id: str, now=None
) -> tuple[bool, int]:
    """May this agent make one more attempt in the current window?

    MUST be called with `lock_agent_ingress` already held: the count and
    the write have to be one decision, or two replicas each permit the
    whole allowance and the limit is a comment.

    Returns ``(permitted, used)``. When permitted, an attempt has been
    RESERVED -- the row exists and the caller records its outcome later.
    When not, nothing was written.
    """
    from harkeniq_cc.db.repos import AgentIngressAttemptRepo

    now = now or datetime.now(timezone.utc)
    repo = AgentIngressAttemptRepo(session)
    window_start = now - timedelta(seconds=ATTEMPT_WINDOW_S)
    # Pruned here rather than on a loop: the window is what makes the
    # count meaningful, so the rows outside it have no other reader.
    await repo.prune(tenant_id, agent_id, window_start)
    used = await repo.count_since(tenant_id, agent_id, window_start)
    if used >= ATTEMPT_MAX:
        # Refused WITHOUT writing. A record that grew on every refusal
        # would be an amplifier on the exact traffic it bounds.
        return False, used
    return True, used
