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

AND THE REFUSAL IS STILL OBSERVED (A27.13)
------------------------------------------
Not recording the refusal in the attempt ledger is right -- a rejection
recorded there would consume the allowance it was refused for. But it
left `throttled` structurally zero, so A27.11's `throttled` state could
never be reached by real traffic and an operator could not tell a silent
runtime from one being refused at the door.

So the rejection is counted in its OWN bounded structure: one row per
(tenant, agent, aligned window), incremented in place. A flood of a
million requests in one minute writes one row. The bound is TIME, not
traffic, which is the property the no-write rule was protecting.
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

    Returns ``(permitted, used)``. It DECIDES; it does not write. The
    caller records the outcome once it knows what happened, which is what
    makes one row per attempt carry a truthful outcome rather than a
    placeholder corrected later. Nothing is written on refusal either --
    that is the bound described above.
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
        # Refused WITHOUT writing to the ATTEMPT ledger. A record that
        # grew on every refusal would be an amplifier on the exact
        # traffic it bounds. The refusal is observed instead by the
        # caller through `record_throttled`, in a structure bounded by
        # time rather than by request count (A27.13).
        return False, used
    # NOT throttling. This request is being SERVED; it merely happens to
    # take the last slot. Marking it would make `throttled` mean
    # "at the limit", which is a different fact and a much commoner one.
    return True, used


# ---------------------------------------------------------------------------
# A27.13: the rejection is observed, bounded by TIME rather than traffic
# ---------------------------------------------------------------------------

#: Bucket granularity for the rejection counter. Aligned like
#: `read_window_start` so replicas agree which bucket a second falls in,
#: and small enough that summing the buckets inside `ATTEMPT_WINDOW_S`
#: answers "throttled in the last hour" to within one bucket.
#:
#: This is what makes the structure bounded: at most one row per agent
#: per minute however many requests arrive in it.
THROTTLE_WINDOW_S = 60

#: How long spent buckets are kept. Wider than the attempt window on
#: purpose -- the projection reads a full `ATTEMPT_WINDOW_S` back, and a
#: horizon close to that could let clock skew between replicas delete a
#: bucket still being reported.
THROTTLE_RETENTION_S = ATTEMPT_WINDOW_S * 3


def throttle_window_start(now=None) -> datetime:
    """The rejection bucket a moment falls in. Fixed-size and aligned."""
    now = now or datetime.now(timezone.utc)
    epoch = int(now.timestamp()) // THROTTLE_WINDOW_S * THROTTLE_WINDOW_S
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


async def record_throttled(
    session: Any, *, tenant_id: str, agent_id: str, now=None
) -> int:
    """Observe that ONE request was actually refused for rate.

    Called only where `admit_attempt` returned False -- i.e. a real
    request received a real 429. Returns the running total for the
    current bucket.

    Housekeeping runs only when a bucket is OPENED, not on every
    rejection: pruning per request would put a DELETE in front of flood
    traffic, which is the shape this whole design exists to avoid.
    """
    from harkeniq_cc.db.repos import AgentThrottleWindowRepo

    now = now or datetime.now(timezone.utc)
    repo = AgentThrottleWindowRepo(session)
    window = throttle_window_start(now)
    total = await repo.record(
        tenant_id=tenant_id, agent_id=agent_id, window_start=window, at=now,
    )
    if total == 1:
        await repo.prune(now - timedelta(seconds=THROTTLE_RETENTION_S))
    return total


async def throttling_observed(
    session: Any, *, tenant_id: str, agent_id: str, now=None
) -> tuple[int, Any]:
    """Rejections inside the attempt window, and the most recent one.

    Read on the SAME horizon the attempt counts use, so an operator sees
    one consistent window rather than two that disagree.
    """
    from harkeniq_cc.db.repos import AgentThrottleWindowRepo

    now = now or datetime.now(timezone.utc)
    return await AgentThrottleWindowRepo(session).observed(
        tenant_id, agent_id,
        throttle_window_start(now - timedelta(seconds=ATTEMPT_WINDOW_S)),
    )


# ---------------------------------------------------------------------------
# A25.6: reads are metered in their own bucket, and in their own SHAPE
# ---------------------------------------------------------------------------
#
# Submissions are counted row-per-attempt because they are rare, governed
# and individually meaningful. Polling is none of those: a row per GET
# would make the meter the largest writer in the system, and mixing the
# two would make "attempts" mean two different things in one column --
# which A25.6 forbids precisely because abuse detection, quotas and
# entitlements will later need to tell them apart.
#
# So reads are counted as a windowed COUNTER: one row per
# (tenant, agent, window), incremented atomically. Distinct bucket,
# distinct shape, and the governed submission ledger is untouched.

READ_WINDOW_S = 60
READ_MAX_PER_WINDOW = 120

#: How long a spent window is kept before housekeeping drops it. Several
#: windows wide on purpose: the limit only ever consults the CURRENT one,
#: and a horizon close to the window size could let clock skew between
#: replicas delete a window still being counted.
READ_RETENTION_S = 3600


def read_window_start(now=None) -> datetime:
    """The window a moment falls in. Fixed-size and aligned, so two
    replicas counting the same second agree on which bucket it is."""
    now = now or datetime.now(timezone.utc)
    epoch = int(now.timestamp()) // READ_WINDOW_S * READ_WINDOW_S
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


async def admit_read(
    session: Any, *, tenant_id: str, agent_id: str, now=None
) -> tuple[bool, int]:
    """May this agent make one more status read in the current window?

    Atomic by construction: the increment is a single UPDATE, and the
    first read of a window inserts under the unique constraint, so two
    replicas cannot both believe they created it. Returns
    ``(permitted, used_after)``.
    """
    from harkeniq_cc.db.repos import AgentReadWindowRepo

    window = read_window_start(now)
    used = await AgentReadWindowRepo(session).increment(
        tenant_id=tenant_id, agent_id=agent_id, window_start=window,
    )
    return used <= READ_MAX_PER_WINDOW, used
