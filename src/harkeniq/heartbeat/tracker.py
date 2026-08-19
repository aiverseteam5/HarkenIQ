"""Peer liveness tracking (Doc 06 §9.3, §9.4).

Peers are statically configured in R1 (config ``peers`` list). A peer is
ALIVE while heartbeats arrive, and becomes UNRESPONSIVE after
``heartbeat.interval x heartbeat.timeout_multiplier`` seconds of silence
(default 10s x 3 = 30s). The last 60 seconds of received health
summaries are retained per peer as pre-failure witness evidence.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from harkeniq.models import HeartbeatPacket, Peer, PeerStatus

logger = logging.getLogger("harkeniq.heartbeat")

#: Seconds of health summaries retained as pre-failure evidence (Doc 06 §9.4).
HEALTH_BUFFER_SECONDS = 60.0


@dataclass
class PeerEvent:
    """A peer liveness transition."""

    peer: Peer
    old_status: PeerStatus
    new_status: PeerStatus
    at: float  # unix timestamp


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class PeerTracker:
    """Registry of configured peers and their liveness state."""

    def __init__(self, config: Optional[dict] = None) -> None:
        config = config or {}
        hb = config.get("heartbeat") or {}
        self.interval: float = hb.get("interval", 10)
        self.timeout_multiplier: int = hb.get("timeout_multiplier", 3)
        self._peers: dict[str, Peer] = {}  # keyed by host
        self._last_beat_ts: dict[str, float] = {}
        self._last_seq: dict[str, int] = {}
        self._health_ts: dict[str, list[float]] = {}  # parallel to health_buffer

        for entry in config.get("peers") or []:
            host = entry["host"]
            self._peers[host] = Peer(
                peer_id="",  # learned from the first heartbeat
                host=host,
                port=entry.get("port", hb.get("port", 5150)),
            )

    @property
    def timeout_seconds(self) -> float:
        return self.interval * self.timeout_multiplier

    # -- receive path -------------------------------------------------------

    def record_heartbeat(
        self, packet: HeartbeatPacket, host: str, now: float
    ) -> Optional[PeerEvent]:
        """Record an authenticated heartbeat from ``host``.

        Returns a PeerEvent when the peer's status transitioned, else None.
        Packets from unconfigured hosts are ignored (R1 static peer list).
        """
        peer = self._peers.get(host)
        if peer is None:
            logger.warning("Heartbeat from unconfigured peer %s ignored", host)
            return None

        # Drop stale/duplicate datagrams (UDP reordering)
        last_seq = self._last_seq.get(host)
        if last_seq is not None and packet.seq <= last_seq:
            return None
        self._last_seq[host] = packet.seq

        peer.peer_id = packet.agent_id
        peer.name = packet.name
        peer.last_heartbeat = _iso(now)
        peer.last_known_health = dict(packet.health_summary)
        self._last_beat_ts[host] = now

        # Retain up to 60s of health summaries as witness evidence
        peer.health_buffer.append(dict(packet.health_summary))
        ts_list = self._health_ts.setdefault(host, [])
        ts_list.append(now)
        while ts_list and now - ts_list[0] > HEALTH_BUFFER_SECONDS:
            ts_list.pop(0)
            peer.health_buffer.pop(0)

        if peer.status != PeerStatus.ALIVE:
            old = peer.status
            peer.status = PeerStatus.ALIVE
            logger.info("Peer %s (%s) is alive", peer.name or host, host)
            return PeerEvent(peer, old, PeerStatus.ALIVE, now)
        return None

    # -- liveness check -----------------------------------------------------

    def check_liveness(self, now: float) -> list[PeerEvent]:
        """Mark peers UNRESPONSIVE after timeout_seconds of silence.

        Peers that never sent a heartbeat stay UNKNOWN.
        """
        events: list[PeerEvent] = []
        for host, peer in self._peers.items():
            if peer.status != PeerStatus.ALIVE:
                continue
            last = self._last_beat_ts.get(host)
            if last is None or now - last > self.timeout_seconds:
                peer.status = PeerStatus.UNRESPONSIVE
                logger.warning(
                    "Peer %s unresponsive (no heartbeat for %.0fs)",
                    peer.name or host, now - last if last else 0,
                )
                events.append(
                    PeerEvent(peer, PeerStatus.ALIVE, PeerStatus.UNRESPONSIVE, now)
                )
        return events

    # -- accessors / persistence -------------------------------------------

    def get_peers(self) -> list[Peer]:
        return list(self._peers.values())

    def get_peer(self, host: str) -> Optional[Peer]:
        return self._peers.get(host)

    def restore_peers(self, peers: list[Peer]) -> None:
        """Restore peer state from a checkpoint for configured hosts.

        Restored ALIVE peers are downgraded to UNKNOWN: the agent was down,
        so their current liveness is unknown until the next heartbeat.
        """
        for saved in peers:
            peer = self._peers.get(saved.host)
            if peer is None:
                continue
            peer.peer_id = saved.peer_id
            peer.name = saved.name
            peer.last_heartbeat = saved.last_heartbeat
            peer.last_known_health = saved.last_known_health
            peer.status = (
                PeerStatus.UNKNOWN if saved.status == PeerStatus.ALIVE else saved.status
            )
