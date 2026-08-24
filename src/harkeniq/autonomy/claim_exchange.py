"""Claim broadcast + ack reliability layer (R3b-2 Phase 3).

ClaimExchange manages outbound claim retransmission and inbound claim
deduplication over UDP.  Claims use the message envelope (type 0x02/0x03)
on the existing heartbeat port.

R-M19: An isolated node (no reachable peers) cannot claim — it reports
on itself via any remaining path.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from harkeniq.autonomy.claim import Claim, ClaimAck
from harkeniq.autonomy.peer_keyring import PeerKeyRing

logger = logging.getLogger("harkeniq.autonomy.claim_exchange")

# Defaults (configurable via config dict, compressed in tests)
DEFAULT_RETRY_INTERVAL = 1.0   # seconds between retransmits
DEFAULT_MAX_RETRIES = 5


@dataclass
class _OutboundClaim:
    """Tracks retransmission state for an outbound claim."""

    claim: Claim
    pending_acks: set[str]     # agent_ids that haven't acked
    retries_left: int
    next_retry_at: float
    acked_by: set[str] = field(default_factory=set)

    @property
    def fully_acked(self) -> bool:
        return len(self.pending_acks) == 0

    @property
    def exhausted(self) -> bool:
        return self.retries_left <= 0 and not self.fully_acked


class ClaimExchange:
    """Manages claim broadcast, retransmission, and ack tracking.

    The exchange does NOT decide who wins — that is the ClaimManager's
    job (Phase 4).  This layer handles reliable delivery.
    """

    def __init__(
        self,
        keyring: PeerKeyRing,
        get_reachable_peers: Callable[[], int],
        get_peer_ids: Callable[[], list[str]],
        retry_interval: float = DEFAULT_RETRY_INTERVAL,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        self._keyring = keyring
        self._get_reachable_peers = get_reachable_peers
        self._get_peer_ids = get_peer_ids
        self._retry_interval = retry_interval
        self._max_retries = max_retries

        # Outbound: claims we've broadcast, keyed by claim_id
        self._outbound: dict[str, _OutboundClaim] = {}

        # Inbound: claims received from peers, keyed by claim_id (dedup)
        self._inbound: dict[str, Claim] = {}

        # Inbound acks for our claims, keyed by claim_id
        self._received_acks: dict[str, list[ClaimAck]] = {}

    def broadcast(self, claim: Claim) -> Optional[str]:
        """Queue a claim for broadcast to all reachable peers.

        Returns the claim_id on success, or None if isolated (R-M19).
        The caller must provide a signed claim.
        """
        if self._get_reachable_peers() == 0:
            logger.warning(
                "Cannot broadcast claim %s: no reachable peers (R-M19)",
                claim.claim_id,
            )
            return None

        peer_ids = self._get_peer_ids()
        if not peer_ids:
            return None

        self._outbound[claim.claim_id] = _OutboundClaim(
            claim=claim,
            pending_acks=set(peer_ids),
            retries_left=self._max_retries,
            next_retry_at=0.0,  # send immediately
        )
        logger.info(
            "Queued claim %s for %s (subject=%s, peers=%d)",
            claim.claim_id, claim.claimant_id,
            claim.subject_device_id, len(peer_ids),
        )
        return claim.claim_id

    def get_claims_to_send(self, now: Optional[float] = None) -> list[Claim]:
        """Return claims that need (re)transmission now.

        Called by the agent loop to get claims that should be sent over UDP.
        """
        now = time.time() if now is None else now
        to_send: list[Claim] = []
        expired_ids: list[str] = []

        for claim_id, out in self._outbound.items():
            if out.fully_acked:
                expired_ids.append(claim_id)
                continue
            if out.exhausted:
                logger.warning(
                    "Claim %s: max retries exhausted, %d peers unacked",
                    claim_id, len(out.pending_acks),
                )
                expired_ids.append(claim_id)
                continue
            if now >= out.next_retry_at:
                to_send.append(out.claim)
                out.retries_left -= 1
                out.next_retry_at = now + self._retry_interval

        for cid in expired_ids:
            del self._outbound[cid]

        return to_send

    def receive_claim(self, claim: Claim) -> bool:
        """Process an inbound claim from a peer.

        Returns True if the claim is new (not a duplicate).
        Verifies the Ed25519 signature using the PeerKeyRing.
        """
        # Dedup
        if claim.claim_id in self._inbound:
            return False

        # Verify signature
        pub_key = self._keyring.get_key(claim.claimant_id)
        if pub_key is None:
            logger.warning(
                "Received claim %s from unknown peer %s, dropping",
                claim.claim_id, claim.claimant_id,
            )
            return False

        if not claim.verify(pub_key):
            logger.warning(
                "Claim %s from %s has invalid signature, dropping",
                claim.claim_id, claim.claimant_id,
            )
            return False

        self._inbound[claim.claim_id] = claim
        logger.info(
            "Received valid claim %s from %s (subject=%s)",
            claim.claim_id, claim.claimant_id, claim.subject_device_id,
        )
        return True

    def receive_ack(self, ack: ClaimAck) -> bool:
        """Process an inbound ack for one of our outbound claims.

        Returns True if the ack was relevant (for a known outbound claim).
        """
        out = self._outbound.get(ack.claim_id)
        if out is None:
            return False

        # Verify signature
        pub_key = self._keyring.get_key(ack.acker_id)
        if pub_key is None:
            logger.warning(
                "Ack for claim %s from unknown peer %s, ignoring",
                ack.claim_id, ack.acker_id,
            )
            return False

        if not ack.verify(pub_key):
            logger.warning(
                "Ack for claim %s from %s has invalid signature, ignoring",
                ack.claim_id, ack.acker_id,
            )
            return False

        out.pending_acks.discard(ack.acker_id)
        out.acked_by.add(ack.acker_id)
        self._received_acks.setdefault(ack.claim_id, []).append(ack)
        return True

    def get_pending_inbound(self) -> list[Claim]:
        """Return all received claims not yet consumed.

        After calling this, the claims are removed from the inbound buffer.
        The ClaimManager (Phase 4) processes these.
        """
        claims = list(self._inbound.values())
        self._inbound.clear()
        return claims

    def cancel_outbound(self, claim_id: str) -> None:
        """Cancel an outbound claim (e.g., lost tiebreak)."""
        self._outbound.pop(claim_id, None)

    def is_fully_acked(self, claim_id: str) -> bool:
        """Check if an outbound claim has been acked by all peers."""
        out = self._outbound.get(claim_id)
        if out is None:
            return claim_id not in self._outbound  # removed = done
        return out.fully_acked

    @property
    def outbound_count(self) -> int:
        return len(self._outbound)

    @property
    def inbound_count(self) -> int:
        return len(self._inbound)
