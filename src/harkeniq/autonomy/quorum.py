"""Quorum disambiguation engine (R3b-2 Phase 5, spec §3.4).

When a device stops responding, four materially different situations
produce the identical signal.  Only peers can separate them:

  | Observation                                             | Conclusion     |
  |-------------------------------------------------------- |----------------|
  | All neighbours lost device; neighbours reach each other | DEVICE_DOWN    |
  | One neighbour lost it; others still reach it            | LINK_DOWN      |
  | Link up, traffic forwards, node silent                  | NODE_FAILED    |
  | One node lost every neighbour simultaneously            | ISOLATED       |

R-M13: No incident on single-node evidence where corroboration is obtainable.
R-M14: Liveness conclusions require 2+ independent observers.
"""

from __future__ import annotations

import logging
from typing import Optional

from harkeniq.models import PeerStatus, QuorumVerdict

logger = logging.getLogger("harkeniq.autonomy.quorum")

# Minimum observers for a quorum verdict (R-M14)
MIN_OBSERVERS = 2


class QuorumEngine:
    """Four-way quorum disambiguation from peer liveness state."""

    def __init__(self, my_agent_id: str) -> None:
        self._my_id = my_agent_id

    def disambiguate(
        self,
        suspect_device_id: str,
        peer_views: dict[str, dict],
        my_peers_alive: int,
        total_peers: int,
    ) -> QuorumVerdict:
        """Determine the nature of a connectivity loss.

        Args:
            suspect_device_id: The device/agent we lost contact with.
            peer_views: {peer_agent_id: {"can_reach_suspect": bool,
                         "is_alive": bool}} — each peer's view.
                is_alive: whether WE can reach this peer.
                can_reach_suspect: whether THIS PEER can reach the suspect.
            my_peers_alive: number of peers we can currently reach.
            total_peers: total number of configured peers.

        Returns:
            QuorumVerdict indicating the disambiguated conclusion.
        """
        if total_peers == 0:
            return QuorumVerdict.INCONCLUSIVE

        # Case 4: Isolated — we lost ALL peers simultaneously
        if my_peers_alive == 0 and total_peers > 0:
            logger.warning(
                "All %d peers lost simultaneously — this node is ISOLATED (R-AGENT-6)",
                total_peers,
            )
            return QuorumVerdict.ISOLATED

        # Filter to peers we can actually reach (alive)
        reachable_views = {
            pid: view for pid, view in peer_views.items()
            if view.get("is_alive", False)
        }

        # R-M14: Need 2+ independent observers for liveness conclusions
        # We count ourselves + reachable peers as observers
        observer_count = 1 + len(reachable_views)  # 1 = ourselves
        if observer_count < MIN_OBSERVERS:
            logger.debug(
                "Insufficient observers for %s: %d < %d (R-M14)",
                suspect_device_id, observer_count, MIN_OBSERVERS,
            )
            return QuorumVerdict.INCONCLUSIVE

        # R-M13: Don't raise incident on single-node evidence
        # where corroboration is obtainable
        if len(reachable_views) == 0:
            return QuorumVerdict.INCONCLUSIVE

        # Count how many reachable peers can/cannot reach the suspect
        peers_that_see_suspect = sum(
            1 for v in reachable_views.values()
            if v.get("can_reach_suspect", False)
        )
        peers_that_lost_suspect = len(reachable_views) - peers_that_see_suspect

        # Case 1: All reachable neighbours also lost the suspect,
        # and they can reach each other → DEVICE_DOWN
        if peers_that_lost_suspect == len(reachable_views) and len(reachable_views) > 0:
            logger.info(
                "DEVICE_DOWN: all %d reachable peers also lost %s",
                len(reachable_views), suspect_device_id,
            )
            return QuorumVerdict.DEVICE_DOWN

        # Case 2: Some peers still reach the suspect → LINK_DOWN
        # (the link between us and the suspect is broken)
        if peers_that_see_suspect > 0 and peers_that_lost_suspect == 0:
            logger.info(
                "LINK_DOWN: %d peers still reach %s, our link is broken",
                peers_that_see_suspect, suspect_device_id,
            )
            return QuorumVerdict.LINK_DOWN

        # Case 3: Mixed — some lost, some see.  This is a partial
        # failure.  If more see than lost, likely LINK_DOWN for the
        # losers; if more lost, likely the device is degrading.
        # For now, if majority see the suspect, it's LINK_DOWN;
        # if majority lost, DEVICE_DOWN.
        if peers_that_see_suspect > peers_that_lost_suspect:
            logger.info(
                "LINK_DOWN (majority): %d/%d peers reach %s",
                peers_that_see_suspect, len(reachable_views),
                suspect_device_id,
            )
            return QuorumVerdict.LINK_DOWN

        if peers_that_lost_suspect > peers_that_see_suspect:
            logger.info(
                "DEVICE_DOWN (majority): %d/%d peers lost %s",
                peers_that_lost_suspect, len(reachable_views),
                suspect_device_id,
            )
            return QuorumVerdict.DEVICE_DOWN

        # Exactly split — inconclusive, need more evidence
        return QuorumVerdict.INCONCLUSIVE

    def check_node_failed(
        self,
        suspect_device_id: str,
        link_up: bool,
        agent_responding: bool,
    ) -> Optional[QuorumVerdict]:
        """Case 3 refinement: link is up but agent is silent.

        Called after disambiguation when LINK_DOWN was not the verdict.
        If the network link to the device is provably up (e.g., ICMP
        responds, BMC reachable) but the agent process isn't sending
        heartbeats, this is NODE_FAILED — the agent process crashed
        but the device hardware is fine (R-AGENT-1).
        """
        if link_up and not agent_responding:
            logger.info(
                "NODE_FAILED: link to %s is up but agent is silent (R-AGENT-1)",
                suspect_device_id,
            )
            return QuorumVerdict.NODE_FAILED
        return None
