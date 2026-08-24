"""Partition fencing (R3b-2 Phase 6, spec R-M19, R-AGENT-6, A2.2).

Enhanced partition detection beyond R3a's lease expiry:

1. Isolated node (all peers lost simultaneously) enters ISOLATED state,
   drops to T2 propose-only, and reports on itself via SM.
2. ClaimManager respects both claim lease AND authorization lease.
3. Isolation detection: all peers UNRESPONSIVE in a single check cycle.
"""

from __future__ import annotations

import logging
from typing import Optional

from harkeniq.models import PeerStatus

logger = logging.getLogger("harkeniq.autonomy.partition_fence")


class PartitionFence:
    """Detects partition/isolation and enforces fencing rules."""

    def __init__(self, my_agent_id: str) -> None:
        self._my_id = my_agent_id
        self._isolated = False
        self._prev_alive_count: Optional[int] = None

    @property
    def is_isolated(self) -> bool:
        return self._isolated

    def check_isolation(
        self,
        peers_alive: int,
        total_peers: int,
        prev_alive: Optional[int] = None,
    ) -> bool:
        """Check if this node has become isolated.

        Isolation means ALL peers became UNRESPONSIVE. If prev_alive
        was >0 and now peers_alive is 0, this is a sudden isolation
        event (likely network partition, not gradual peer loss).

        Returns True if newly isolated (transition detected).
        """
        if total_peers == 0:
            self._isolated = False
            return False

        was_connected = (
            self._prev_alive_count is not None
            and self._prev_alive_count > 0
        )
        if prev_alive is not None:
            was_connected = prev_alive > 0

        now_isolated = peers_alive == 0

        newly_isolated = now_isolated and was_connected and not self._isolated
        if newly_isolated:
            self._isolated = True
            logger.warning(
                "ISOLATED: all %d peers lost (was %s alive). "
                "Entering fenced mode (R-M19, R-AGENT-6)",
                total_peers, self._prev_alive_count or prev_alive,
            )

        # Also enter isolated if we've never been connected and have 0 peers
        if now_isolated and not self._isolated and not was_connected:
            self._isolated = True

        self._prev_alive_count = peers_alive
        return newly_isolated

    def check_recovery(self, peers_alive: int) -> bool:
        """Check if isolation has ended (peers recovered).

        Returns True if recovered (transition from isolated to connected).
        """
        if not self._isolated:
            return False
        if peers_alive > 0:
            self._isolated = False
            self._prev_alive_count = peers_alive
            logger.info(
                "RECOVERED from isolation: %d peers alive", peers_alive
            )
            return True
        return False

    def is_fenced(self, auth_lease_valid: bool) -> bool:
        """Check if this node should be fenced (cannot execute actions).

        A node is fenced if:
        - It is isolated (no peers), OR
        - Its authorization lease has expired
        """
        if self._isolated:
            return True
        if not auth_lease_valid:
            return True
        return False

    def fence_status(self) -> dict:
        """Return current fence status for logging/reporting."""
        return {
            "isolated": self._isolated,
            "prev_alive_count": self._prev_alive_count,
        }
