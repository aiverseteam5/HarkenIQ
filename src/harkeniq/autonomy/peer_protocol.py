"""Peer/mesh protocol abstraction (spec A2.7 contract 7).

Wraps the existing PeerTracker with a stable interface for tier gating
and witness evidence.  R3a uses get_reachable_peers() for tier
calculation and get_peer_state() for witness.  R3b-2 implements
broadcast_claim(), receive_claims(), renew_lease(), exchange_suspicion()
for the full mesh protocol.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from harkeniq.autonomy.claim import Claim
from harkeniq.autonomy.claim_exchange import ClaimExchange
from harkeniq.autonomy.claim_manager import ClaimManager
from harkeniq.autonomy.peer_keyring import PeerKeyRing
from harkeniq.heartbeat.tracker import PeerTracker
from harkeniq.models import Peer, PeerStatus

logger = logging.getLogger("harkeniq.autonomy.peer_protocol")


class PeerProtocol:
    """Stable interface over PeerTracker for autonomy subsystem.

    R3a methods (implemented):
      - get_reachable_peers() -> int (for tier gating)
      - get_peer_state(peer_id) -> health buffer (for witness evidence)
      - get_all_peers() -> list[Peer]

    R3b-2 methods (implemented):
      - broadcast_claim(subject, evidence) -> Optional[str]
      - receive_claims() -> list[Claim]
      - renew_lease(claim_id) -> bool
      - exchange_suspicion(component, score) -> None
    """

    def __init__(
        self,
        tracker: Optional[PeerTracker] = None,
        agent_id: str = "",
        keyring: Optional[PeerKeyRing] = None,
        identity=None,
        claim_config: Optional[dict] = None,
    ) -> None:
        self._tracker = tracker
        self._agent_id = agent_id
        self._keyring = keyring or PeerKeyRing()
        self._identity = identity  # AgentIdentity for signing
        self._claim_seq = 0

        # R3b-2: claim exchange and manager
        claim_cfg = claim_config or {}
        self._exchange: Optional[ClaimExchange] = None
        self._manager: Optional[ClaimManager] = None

        if agent_id:
            self._manager = ClaimManager(
                my_agent_id=agent_id,
                claim_lease_duration=claim_cfg.get("lease_duration", 120.0),
            )
            self._exchange = ClaimExchange(
                keyring=self._keyring,
                get_reachable_peers=self.get_reachable_peers,
                get_peer_ids=self._get_alive_peer_ids,
                retry_interval=claim_cfg.get("retry_interval", 1.0),
                max_retries=claim_cfg.get("max_retries", 5),
            )

        # R3b-2 Phase 5: suspicion and quorum
        self._suspicion_tracker = None
        self._quorum_engine = None
        if agent_id:
            from harkeniq.autonomy.quorum import QuorumEngine
            self._quorum_engine = QuorumEngine(my_agent_id=agent_id)

    # -- R3a methods (unchanged) -----------------------------------------------

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

    # -- R3b-2: claim protocol (Phase 4) --------------------------------------

    def broadcast_claim(self, subject: str, evidence: dict) -> Optional[str]:
        """Broadcast an incident claim to peers.

        Returns claim_id on success, None if isolated (R-M19) or
        mesh not initialized.
        """
        if self._exchange is None or self._manager is None:
            return None
        if self._identity is None:
            logger.warning("Cannot broadcast claim: no agent identity")
            return None

        self._claim_seq += 1
        claim = Claim.new(
            claimant_id=self._agent_id,
            subject_device_id=subject,
            evidence=evidence,
            seq=self._claim_seq,
        )
        claim.sign(self._identity._private_key)

        # Process locally first
        result = self._manager.process_claim(claim)
        if result == "rejected":
            return None

        # Broadcast to peers
        return self._exchange.broadcast(claim)

    def receive_claims(self) -> list[Claim]:
        """Receive and process incident claims from peers.

        Returns list of newly accepted claims. The ClaimManager decides
        ownership via deterministic tiebreak.
        """
        if self._exchange is None or self._manager is None:
            return []

        inbound = self._exchange.get_pending_inbound()
        accepted: list[Claim] = []
        for claim in inbound:
            result = self._manager.process_claim(claim)
            if result in ("accepted", "superseded"):
                accepted.append(claim)
        return accepted

    def renew_lease(self, claim_id: str) -> bool:
        """Renew ownership lease on a claimed incident."""
        if self._manager is None:
            return False
        return self._manager.renew_lease(claim_id)

    # -- R3b-2: suspicion exchange (Phase 5 stub — no longer raises) ----------

    def exchange_suspicion(self, component: str, score: float) -> None:
        """Exchange suspicion state with peers for trending (R-M20).

        Full implementation in Phase 5. Does nothing until suspicion
        tracker is attached.
        """
        if self._suspicion_tracker is not None:
            self._suspicion_tracker.update_local(component, score)

    # -- R3b-2: helpers -------------------------------------------------------

    def _get_alive_peer_ids(self) -> list[str]:
        """Return agent_ids of all ALIVE peers."""
        if self._tracker is None:
            return []
        return [
            p.peer_id for p in self._tracker.get_peers()
            if p.status == PeerStatus.ALIVE and p.peer_id
        ]

    @property
    def claim_manager(self) -> Optional[ClaimManager]:
        return self._manager

    @property
    def claim_exchange(self) -> Optional[ClaimExchange]:
        return self._exchange
