"""SHA-256 hash chain for audit trails (R4-2 P12, OQ-20).

Storage-agnostic chain primitives shared by all four audit stores (agent
checkpoint sqlite, SM/CC/Console SQLAlchemy). Each service keeps ONE
chain per audit table with its own monotonic sequence starting at 1:

    entry_hash = SHA-256(canonical_json({
        "seq": seq, "prev": prev_hash, "payload": payload}))

where entry 1 links to GENESIS_HASH (64 zeros). Tampering with any
stored entry (payload, order, deletion, insertion) breaks recomputation
at or after that point.

OQ-20 resolution (design decision, R4-2): the chain is computed at
WRITE time (cheap: one hash per append); verification is EXPLICIT and
on-demand (API endpoint / repo method / CLI), never on the read path.
Verify-on-read would cost O(chain length) per query and adds nothing --
a reader who does not trust the store must run full verification anyway.

Concurrency: appenders must serialize (read tail -> hash -> insert).
Single-process services use an asyncio.Lock plus a UNIQUE constraint on
seq as a backstop -- a racing append fails loudly instead of forking
the chain. Multi-replica deployments additionally take a PostgreSQL
transaction-scoped advisory lock (R5-2, pg_advisory_chain_lock below):
the lock is held from tail-read through the caller's commit, so two
replicas can never interleave a chain append.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

#: prev_hash of the first entry in every chain.
GENESIS_HASH = "0" * 64


def canonical_json(data: Mapping[str, Any]) -> bytes:
    """Deterministic JSON encoding (sorted keys, no whitespace, UTF-8).

    Same encoding used by agent identity, mesh claims, heartbeat HMAC,
    and license fingerprints. Promoted to a public shared helper here.
    """
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")


def compute_entry_hash(seq: int, prev_hash: str, payload: Mapping[str, Any]) -> str:
    """SHA-256 hex digest binding an entry to its position and predecessor."""
    envelope = {"seq": seq, "prev": prev_hash, "payload": dict(payload)}
    return hashlib.sha256(canonical_json(envelope)).hexdigest()


def next_link(
    tail_seq: int, tail_hash: Optional[str], payload: Mapping[str, Any]
) -> tuple[int, str, str]:
    """Compute (seq, prev_hash, entry_hash) for the next entry.

    tail_seq is 0 and tail_hash None/empty for an empty chain.
    """
    seq = tail_seq + 1
    prev_hash = tail_hash or GENESIS_HASH
    return seq, prev_hash, compute_entry_hash(seq, prev_hash, payload)


@dataclass
class ChainVerification:
    """Result of verifying a full chain."""

    valid: bool
    length: int
    first_bad_seq: Optional[int] = None
    error: str = ""


def advisory_lock_key(chain_name: str) -> int:
    """Stable signed 64-bit key for pg_advisory_xact_lock, derived from
    the chain's name (e.g. "cc.cc_audit_log")."""
    digest = hashlib.sha256(chain_name.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


async def pg_advisory_chain_lock(session, chain_name: str) -> bool:
    """Serialize chain appends across replicas on PostgreSQL (R5-2).

    Takes a TRANSACTION-scoped advisory lock (pg_advisory_xact_lock):
    held from here through the caller's commit/rollback, so the
    tail-read + insert + commit sequence is atomic across replicas.
    Returns True when the lock was taken; False (no-op) on non-Postgres
    dialects, where the per-process asyncio.Lock is sufficient (sqlite
    is single-writer anyway).

    SQLAlchemy is imported lazily: the core package does not depend on
    it, and this helper is only callable from the services (which do).
    """
    bind = getattr(session, "bind", None)
    dialect = getattr(getattr(bind, "dialect", None), "name", "")
    if dialect != "postgresql":
        return False
    from sqlalchemy import text

    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": advisory_lock_key(chain_name)},
    )
    return True


def verify_chain(
    entries: Iterable[tuple[int, str, str, Mapping[str, Any]]],
) -> ChainVerification:
    """Verify a full chain of (seq, prev_hash, entry_hash, payload) rows.

    Rows must be supplied in ascending seq order. Checks:
      - seq starts at 1 and increments by exactly 1 (no gaps/reorders)
      - entry 1 links to GENESIS_HASH; entry N links to hash of N-1
      - every entry_hash recomputes from (seq, prev_hash, payload)

    An empty chain is valid.
    """
    expected_seq = 1
    expected_prev = GENESIS_HASH
    count = 0
    for seq, prev_hash, entry_hash, payload in entries:
        if seq != expected_seq:
            return ChainVerification(
                valid=False, length=count, first_bad_seq=seq,
                error=f"sequence gap: expected seq {expected_seq}, got {seq}",
            )
        if prev_hash != expected_prev:
            return ChainVerification(
                valid=False, length=count, first_bad_seq=seq,
                error=f"broken link at seq {seq}: prev_hash does not match "
                      f"hash of seq {seq - 1}",
            )
        recomputed = compute_entry_hash(seq, prev_hash, payload)
        if recomputed != entry_hash:
            return ChainVerification(
                valid=False, length=count, first_bad_seq=seq,
                error=f"entry_hash mismatch at seq {seq}: payload or hash "
                      f"was modified",
            )
        expected_prev = entry_hash
        expected_seq += 1
        count += 1
    return ChainVerification(valid=True, length=count)
