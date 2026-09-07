"""A27 (A6-3): who caused this proposal, and how is that runtime doing.

A6-1 gave an external runtime a governed way to WRITE and A6-2 gave it a
governed way to READ. This module owns the third side of the same
interaction: the **human's** ability to attribute and supervise it.

TWO FACTS, ONE CONCERN
----------------------
`origin` reached `admit_proposal()` and was written ONLY into the
audit-entry detail JSON -- no column, no API payload -- while
`/api/approvals/` already spends the word `origin` on the queue LANE
(`node` / `agent` / `agent_activation` / `campaign_wave`). So an approver
saw `"agent"` for a proposal HarkenIQ reasoned itself AND for one an
external runtime asked for. And `cc_agent_submissions`,
`cc_agent_ingress_attempts` and `cc_agent_read_windows` were written by
the submit route and the meter and read by NOTHING else, so nobody could
say whether a runtime was submitting, refused, throttled or silent.

Both are the same house pattern: declared, written, and unreadable at the
point where somebody must act on it.

WHAT THIS MODULE WILL NOT DO
----------------------------
It answers no authorization question. Provenance is not an authorization
input -- an externally submitted proposal is governed identically to an
internally derived one -- and ingress health is operational state, not
approval authority. Every caller here has already been authorized by the
route guard and the canonical scope resolver.

It also invents no connectivity. HarkenIQ holds no heartbeat, session or
connection signal for an external runtime, so it claims none (A27.10).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from harkeniq_cc.proposal_admission import (
    ORIGIN_EVALUATOR, ORIGIN_INGRESS, PROVENANCE_TYPES, PROVENANCE_UNKNOWN,
)

# ---------------------------------------------------------------------------
# Provenance projection (A27.6)
# ---------------------------------------------------------------------------

#: What a provenance block may EVER carry. Named so a test can sweep it
#: rather than trusting that no future field creeps in: A27.6 permits the
#: type and, for an external submission only, the submission id A6-1
#: already persisted as authoritative. No credential, no client id, no
#: secret, no token, no realm subject.
PROVENANCE_FIELDS: frozenset[str] = frozenset({"type", "submission_id"})


def provenance_type(proposal) -> str:
    """The stored discriminator, or `unknown`. One reading rule.

    A27.4: NULL is not a missing value to be guessed at -- it is a row
    created before A6-3, which has no authoritative provenance. It reads
    `unknown` and is never manufactured into `evaluator`.

    A value outside the closed vocabulary also reads `unknown`: a
    discriminator this code does not recognise is exactly as unknown as
    an absent one, and reporting it verbatim would let a future writer
    put an unvetted string on a human decision surface.
    """
    stored = getattr(proposal, "provenance_type", None) or ""
    return stored if stored in PROVENANCE_TYPES else PROVENANCE_UNKNOWN


def provenance_block(proposal, submission_id: str = "") -> dict[str, Any]:
    """Who caused this proposal to exist, for a HUMAN surface (A27.6).

    Built by NAMING what may pass, the rule A25.9 established after the
    machine surface leaked by carrying a payload written for somebody
    else.

    `submission_id` is included only for `external_ingress`, only because
    A6-1 persisted it as authoritative, and only so an operator can
    correlate a decision with the submission that asked for it. It is not
    a credential and confers nothing -- A25.2 already established that
    possessing one proves nothing without being the agent that made it.
    """
    kind = provenance_type(proposal)
    block: dict[str, Any] = {"type": kind}
    if kind == ORIGIN_INGRESS and submission_id:
        block["submission_id"] = submission_id
    return block


def submission_ids_for(rows: Iterable[Any]) -> dict[str, str]:
    """proposal_id -> submission_id, from rows already fetched.

    Deliberately takes ROWS rather than issuing its own query: the caller
    fetches once for the whole page (one `IN` over the page's ids), so a
    queue of fifty proposals is one extra query and never fifty.
    """
    out: dict[str, str] = {}
    for row in rows:
        pid = getattr(row, "proposal_id", None)
        if pid:
            out[pid] = row.id
    return out


# ---------------------------------------------------------------------------
# Ingress health (A27.8 - A27.11)
# ---------------------------------------------------------------------------

#: A27.9: the observation windows are the ones that already exist and
#: already prune themselves. Nothing new is retained and no second
#: accounting subsystem is created.
from harkeniq_cc.ingress_limits import (  # noqa: E402 - after the docstring
    ATTEMPT_WINDOW_S, READ_MAX_PER_WINDOW, READ_WINDOW_S, read_window_start,
    throttling_observed,
)

#: A27.9: the refusal sample. `cc_agent_submissions` is the DURABLE
#: idempotency ledger and is deliberately never pruned, so it is the one
#: structure that must never be scanned whole. Bounded, deterministic
#: ordering, fixed limit.
REFUSAL_SAMPLE = 20

#: Bounds on the projected refusal text. `code` is server-minted from a
#: closed set; `reason` is server-composed but is truncated anyway, so a
#: future writer cannot put an essay on an operator's screen.
REASON_LIMIT = 256

#: A27.11: how "recently" is measured, in one place.
ACTIVE_RECENTLY_S = 3600
IDLE_S = 86400

#: A27.11: the derived summary, and the ONLY values it may take.
STATE_NEVER_AUTHENTICATED = "never_authenticated"
STATE_THROTTLED = "throttled"
STATE_REPEATEDLY_REFUSED = "repeatedly_refused"
STATE_ACTIVE_RECENTLY = "active_recently"
STATE_IDLE = "idle"
STATE_NO_RECENT_ACTIVITY = "no_recent_activity"

ACTIVITY_STATES: tuple[str, ...] = (
    STATE_NEVER_AUTHENTICATED,
    STATE_THROTTLED,
    STATE_REPEATEDLY_REFUSED,
    STATE_ACTIVE_RECENTLY,
    STATE_IDLE,
    STATE_NO_RECENT_ACTIVITY,
)


def _iso(value) -> Optional[str]:
    return value.isoformat() if value else None


def _aware(value):
    """Timestamps compared against an aware `now`, whatever the engine.

    sqlite hands back naive datetimes for a `DateTime(timezone=True)`
    column and PostgreSQL hands back aware ones. Comparing the two raises,
    and this function is the reason `activity_state` cannot differ
    between the unit suite and production.
    """
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def activity_state(
    *,
    last_authenticated_at,
    last_attempt_at,
    accepted: int,
    refused: int,
    throttled: int,
    now=None,
) -> str:
    """One word for an operator scanning a list. NEVER an authority.

    A27.11 fixes this precedence BEFORE the code so two overlapping
    conditions cannot produce two answers, and the raw counts and
    timestamps travel beside it so anything that needs to reason reads
    those instead.

    First match wins:

      1. never_authenticated   no authentication and no attempt, ever
      2. throttled             the runtime is being refused for rate
      3. repeatedly_refused    it is trying and nothing is landing
      4. active_recently       any observation within the last hour
      5. idle                  any observation within the last day
      6. no_recent_activity    otherwise
    """
    now = now or datetime.now(timezone.utc)
    auth_at = _aware(last_authenticated_at)
    attempt_at = _aware(last_attempt_at)

    if auth_at is None and attempt_at is None:
        return STATE_NEVER_AUTHENTICATED
    if throttled > 0:
        return STATE_THROTTLED
    if refused > 0 and accepted == 0:
        return STATE_REPEATEDLY_REFUSED

    latest = max([t for t in (auth_at, attempt_at) if t is not None])
    age = (now - latest).total_seconds()
    if age <= ACTIVE_RECENTLY_S:
        return STATE_ACTIVE_RECENTLY
    if age <= IDLE_S:
        return STATE_IDLE
    return STATE_NO_RECENT_ACTIVITY


async def build_ingress_health(
    session: Any, *, tenant_id: str, agent, identity=None, now=None
) -> dict[str, Any]:
    """What this Operational Agent's ingress actually looks like (A27.8).

    Every number here is an aggregate over a window that already exists
    and already prunes itself, or a `LIMIT`-bounded sample. Nothing scans
    the durable submission ledger whole.

    `identity` is the agent's `cc_agent_identities` row, or None where it
    has no machine credential -- an agent that was never credentialed has
    no authentication to report, and saying so is the honest answer.
    """
    from harkeniq_cc.db.repos import (
        AgentIngressAttemptRepo, AgentReadWindowRepo, AgentSubmissionRepo,
    )

    now = now or datetime.now(timezone.utc)
    window_start = now - timedelta(seconds=ATTEMPT_WINDOW_S)

    attempts = await AgentIngressAttemptRepo(session).counts_since(
        tenant_id, agent.id, window_start,
    )
    last_attempt_at = await AgentIngressAttemptRepo(session).last_attempt_at(
        tenant_id, agent.id,
    )
    reads_used = await AgentReadWindowRepo(session).usage(
        tenant_id=tenant_id, agent_id=agent.id,
        window_start=read_window_start(now),
    )
    # A29.16: the refusal evidence A6-4A records has no other reader. It
    # answers the operator's question -- WHICH runtime is being refused,
    # how often, for which closed reason, and how recently -- and it
    # belongs on this contract rather than a second one, because a second
    # telemetry vocabulary for the same runtime is how a surface ends up
    # answering neither question.
    #
    # Read on the SAME horizon as the attempt counts, so an operator sees
    # one window rather than several that disagree.
    # Named distinctly: `refusals` below is the submission refusal SAMPLE,
    # a different fact on a different ledger. Reusing the name silently
    # overwrote this dict, which is the kind of collision a projection
    # built by naming what may pass is supposed to make impossible.
    surface_counts, surface_last_at = await AgentReadWindowRepo(
        session
    ).refusals_since(
        tenant_id, agent.id, read_window_start(window_start),
    )
    refusals, last_accepted_at = await AgentSubmissionRepo(session).recent_refusals(
        tenant_id, agent.id, limit=REFUSAL_SAMPLE,
    )

    accepted = int(attempts.get("accepted", 0))
    refused = sum(
        int(v) for k, v in attempts.items()
        if k in ("rejected", "refused", "conflict")
    )
    # A27.13: NOT from the attempt ledger. A rate rejection deliberately
    # never becomes an attempt row -- it would consume the allowance it
    # was refused for -- so this used to read a key the vocabulary does
    # not contain and was zero forever, which made `activity_state`
    # structurally unable to reach `throttled` in production. It comes
    # from the bounded rejection counter, on the same window as the
    # attempt counts so an operator sees one horizon, not two.
    throttled, last_throttled_at = await throttling_observed(
        session, tenant_id=tenant_id, agent_id=agent.id, now=now,
    )
    last_auth = getattr(identity, "last_seen_at", None) if identity else None

    return {
        "agent_id": agent.id,
        "tenant_id": tenant_id,
        # A27.10: the last authoritative PERSISTED authentication
        # observation -- written by `AgentIdentityRepo.touch()` on every
        # authenticated machine request. It does NOT mean a connection
        # exists, and nothing here ever says "connected".
        "last_authenticated_at": _iso(last_auth),
        "credentialed": identity is not None,
        "identity_status": getattr(identity, "status", "") if identity else "none",
        "submission_activity": {
            "window_seconds": ATTEMPT_WINDOW_S,
            "attempts": sum(int(v) for v in attempts.values()),
            "accepted": accepted,
            "refused": refused,
            "throttled": throttled,
            "by_outcome": {k: int(v) for k, v in sorted(attempts.items())},
            "last_attempt_at": _iso(last_attempt_at),
            "last_accepted_at": _iso(last_accepted_at),
            # A27.13: when a request was last actually refused for rate.
            # A count answers "how many"; only a timestamp answers "how
            # recently", and an operator deciding whether a runtime is
            # still being throttled needs the second one.
            "last_throttled_at": _iso(last_throttled_at),
        },
        "read_throttle": {
            "window_seconds": READ_WINDOW_S,
            "used": reads_used,
            "limit": READ_MAX_PER_WINDOW,
            "exhausted": reads_used >= READ_MAX_PER_WINDOW,
        },
        # A29.16: refusals at the API PLANE -- a route this runtime may
        # not use, or a job its operator never bound. Distinct from
        # `submission_activity.refused`, which counts governed submissions
        # that were considered and declined; these were never considered
        # at all. Named on the same horizon as the rest of this contract.
        "surface_refusals": {
            "window_seconds": ATTEMPT_WINDOW_S,
            "total": surface_counts["total"],
            # The CLOSED reason vocabulary, one count each. No path, query,
            # body or error text appears here or in the store behind it.
            "by_reason": {
                "surface_not_allowed": surface_counts["surface_not_allowed"],
                "machine_job_not_bound": surface_counts["machine_job_not_bound"],
            },
            "last_surface_refused_at": _iso(surface_last_at),
        },
        # A27.9: bounded sample, newest first, never the whole ledger.
        "recent_refusals": [
            {
                "submission_id": row.id,
                "code": (row.code or "")[:64],
                "reason": (row.reason or "")[:REASON_LIMIT],
                "at": _iso(row.created_at),
            }
            for row in refusals
        ],
        "recent_refusal_limit": REFUSAL_SAMPLE,
        "activity_state": activity_state(
            last_authenticated_at=last_auth,
            last_attempt_at=last_attempt_at,
            accepted=accepted, refused=refused, throttled=throttled, now=now,
        ),
        "governs": (
            "Observed ingress activity. HarkenIQ holds no heartbeat or "
            "session for an external runtime and claims none: "
            "`last_authenticated_at` is the last persisted authentication "
            "observation, not a connection. The counts and timestamps are "
            "authoritative; `activity_state` is a summary over them. "
            "`surface_refusals` counts requests refused at the API plane "
            "-- a route this runtime may not use, or a job its operator "
            "never bound -- which is a different fact from a governed "
            "submission that was considered and declined."
        ),
    }


__all__ = [
    "ACTIVITY_STATES", "PROVENANCE_FIELDS", "REFUSAL_SAMPLE",
    "ORIGIN_EVALUATOR", "ORIGIN_INGRESS", "PROVENANCE_UNKNOWN",
    "activity_state", "build_ingress_health", "provenance_block",
    "provenance_type", "submission_ids_for",
]
