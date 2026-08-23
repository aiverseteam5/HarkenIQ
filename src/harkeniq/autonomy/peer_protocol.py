"""Peer/mesh protocol abstraction (spec A2.7 contract 7).

Wraps the existing PeerTracker with a stable interface for tier gating
and witness evidence.  R3a uses get_reachable_peers() for tier
calculation and get_peer_state() for witness.  R3b extends with
broadcast_claim(), receive_claims(), renew_lease(), exchange_suspicion()
for the full mesh protocol.
"""

from __future__ import annotations

from typing import Any, Optional

from harkeniq.heartbeat.tracker import PeerTracker
from harkeniq.models import Peer, PeerStatus


class PeerProtocol:
    """Stable interface over PeerTracker for autonomy subsystem.

    R3a methods (implemented):
      - get_reachable_peers() -> int (for tier gating)
      - get_peer_state(peer_id) -> health buffer (for witness evidence)
      - get_all_peers() -> list[Peer]

    R3b methods (interface defined, not implemented):
      - broadcast_claim(subject, evidence) -> None
      - receive_claims() -> list[Claim]
      - renew_lease(claim_id) -> None
      - exchange_suspicion(component, score) -> None
    """

    def __init__(self, tracker: Optional[PeerTracker] = None) -> None:
        self._tracker = tracker

    def get_reachable_peers(self) -> int:
        """Count of currently ALIVE peers (for tier calculation)."""
        if self._tracker is None:
            return 0
        return sum(
            1 for p in self._tracker.get_peers()
            if p.status == PeerStatus.ALIVE
        )

    def get_peer_state(self, host: str) -> Optional[dict[str, Any]]:
        """Get the last known health state of a peer (for witness evidence)."""
        if self._tracker is None:
            return None
        peer = self._tracker.get_peer(host)
        if peer is None:
            return None
        return {
            "peer_id": peer.peer_id,
            "host": peer.host,
            "status": peer.status.value,
            "last_heartbeat": peer.last_heartbeat,
            "last_known_health": peer.last_known_health,
            "health_buffer_size": len(peer.health_buffer),
        }

    def get_all_peers(self) -> list[Peer]:
        """All configured peers with their current state."""
        if self._tracker is None:
            return []
        return self._tracker.get_peers()

    # -- R3b interface stubs (not implemented) ------------------------------

    def broadcast_claim(self, subject: str, evidence: dict) -> None:
        """R3b: Broadcast an incident claim to peers."""
        raise NotImplementedError("Full mesh protocol ships in R3b")

    def receive_claims(self) -> list:
        """R3b: Receive incident claims from peers."""
        raise NotImplementedError("Full mesh protocol ships in R3b")

    def renew_lease(self, claim_id: str) -> None:
        """R3b: Renew ownership lease on a claimed incident."""
        raise NotImplementedError("Full mesh protocol ships in R3b")

    def exchange_suspicion(self, component: str, score: float) -> None:
        """R3b: Exchange suspicion state with peers for trending."""
        raise NotImplementedError("Full mesh protocol ships in R3b")
