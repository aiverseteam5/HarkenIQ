"""Tier gating from peer count (spec A2.3, mesh design §2).

T1: >= 2 reachable peers → may act (within budget + lease)
T2: 0-1 reachable peers  → propose only
T3: no agent present     → report only (used by SM for polled devices)

Uses the existing PeerTracker.get_peers() to count ALIVE peers.
"""

from __future__ import annotations

from enum import Enum

from harkeniq.models import PeerStatus


class TierLevel(str, Enum):
    T1 = "t1"  # >= 2 alive peers, may act
    T2 = "t2"  # 0-1 alive peers, propose only
    T3 = "t3"  # no agent, report only (SM poll-path)


def calculate_tier(peers: list) -> TierLevel:
    """Derive tier from current peer liveness state.

    Args:
        peers: List of Peer objects from PeerTracker.get_peers().
    """
    alive = sum(1 for p in peers if p.status == PeerStatus.ALIVE)
    return TierLevel.T1 if alive >= 2 else TierLevel.T2
