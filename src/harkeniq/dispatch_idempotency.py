"""Dispatch idempotency: one proposal, at most one Site Manager directive.

Central Command hands an approved Operational Agent proposal to the Site
Manager with `DispatchAction`. Before PB-0 (spec A30.42/A30.43) a lost
acknowledgement, a second replica, or two approvers finishing together
could each deliver the same proposal, and the Site Manager queued every
one of them -- two directives, two executions, two charges against the
site's error budget.

Two halves close it. Central Command takes a per-proposal claim before it
calls the Site Manager; the Site Manager keys every proposal-driven
directive by the proposal and answers a repeat with the directive it
already holds. This module is the ONE definition of the key and of the
signal the Site Manager uses to say "this was a replay". It lives in the
`harkeniq` package because the Site Manager writes both and Central
Command reads the signal, and two copies of a wire convention are two
conventions.
"""

from __future__ import annotations

from typing import Optional

#: Prefix of a proposal-driven directive's idempotency key.
KEY_PREFIX = "proposal:"

#: `ActionDispatchAck.reason` on an ACCEPTED ack when the Site Manager
#: answered with a directive it already held instead of queueing a new
#: one. `reason` was empty on every accepted ack before PB-0, so an old
#: Central Command reading a new Site Manager sees nothing new, and no
#: proto field was needed.
REPLAY_REASON = "idempotent_replay"


def idempotency_key(proposal_id: str) -> Optional[str]:
    """The directive key for `proposal_id`, or None when there is none.

    No proposal id means the dispatch is not proposal-driven (a campaign
    wave target, say): it carries no key and behaves exactly as before.
    """
    proposal_id = (proposal_id or "").strip()
    return f"{KEY_PREFIX}{proposal_id}" if proposal_id else None


def is_replay(ack: dict) -> bool:
    """Did an accepted ack name a directive the Site Manager already held?"""
    return bool(ack.get("accepted")) and ack.get("reason") == REPLAY_REASON
