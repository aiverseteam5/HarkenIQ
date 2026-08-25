"""Network action safety: self-preservation + fault-domain limits (R6-P6).

Two mechanisms the A9 action table depends on, both fail-closed:

1. **ManagementPathResolver** (design doc §7 decision 4 / review 3A): at
   precondition time, resolve which interfaces carry the agent's own path
   to its Site Manager — kernel route lookup toward the SM address, then
   LAG expansion. An action targeting any interface in that set is refused;
   *inability to resolve is itself a refusal* (a safety check that cannot
   prove safety refuses, like every other A2.1 precondition). Refusals are
   recorded with reasons (R-M11) by the caller.

2. **NetworkActionTracker** (decision 10): blast-radius bookkeeping keyed
   on the fault-domain hierarchy (port -> LAG -> switch). v1 enforces:
   never two ports of one LAG inside the window, and at most one
   disruptive port action per switch per window (1/fault-domain/30min).
   Agent-local; the SM enforces site-wide limits independently.

The resolver's route reader is injectable: production reads the Linux
routing table (/proc/net/route); tests inject a fake. On non-Linux or
unreadable systems the resolver returns None -> fail closed.
"""

from __future__ import annotations

import logging
import socket
import struct
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger("harkeniq.autonomy.network_safety")

#: One disruptive action per fault domain per this window (A9 D6).
DOMAIN_WINDOW_S = 1800.0


def _read_proc_net_route() -> list[tuple[str, str, str]]:
    """Read Linux routes as (destination_ip, mask_ip, interface) tuples."""
    routes = []
    with open("/proc/net/route") as f:
        next(f)  # header
        for line in f:
            parts = line.split()
            if len(parts) < 8:
                continue
            iface, dest_hex, _gw, _flags, _r, _u, _m, mask_hex = parts[:8]
            dest = socket.inet_ntoa(struct.pack("<I", int(dest_hex, 16)))
            mask = socket.inet_ntoa(struct.pack("<I", int(mask_hex, 16)))
            routes.append((dest, mask, iface))
    return routes


def _ip_in_subnet(ip: str, dest: str, mask: str) -> bool:
    ip_i = struct.unpack("!I", socket.inet_aton(ip))[0]
    dest_i = struct.unpack("!I", socket.inet_aton(dest))[0]
    mask_i = struct.unpack("!I", socket.inet_aton(mask))[0]
    return (ip_i & mask_i) == (dest_i & mask_i)


class ManagementPathResolver:
    """Resolve the interface set carrying the agent's SM session."""

    def __init__(
        self,
        sm_host: str,
        lag_members: Optional[dict[str, list[str]]] = None,
        route_reader: Callable[[], list[tuple[str, str, str]]] = _read_proc_net_route,
        resolve_host: Callable[[str], str] = None,
    ) -> None:
        self.sm_host = sm_host
        self.lag_members = lag_members or {}
        self._route_reader = route_reader
        self._resolve_host = resolve_host or (
            lambda host: socket.gethostbyname(host)
        )

    def management_interfaces(self) -> Optional[set[str]]:
        """The interfaces that would sever SM connectivity if disabled.

        Returns None when resolution fails for ANY reason — the caller
        must treat None as "cannot prove safety" and refuse (fail-closed,
        review 3A). Never returns a guessed set.
        """
        try:
            sm_ip = self._resolve_host(self.sm_host)
            routes = self._route_reader()
        except Exception as e:  # noqa: BLE001 — any failure means "unknown"
            logger.warning(
                "management-path resolution failed (%s): fail-closed", e
            )
            return None
        if not routes:
            return None
        # Longest-prefix match toward the SM address; fall back to default.
        best: Optional[tuple[int, str]] = None
        for dest, mask, iface in routes:
            try:
                if _ip_in_subnet(sm_ip, dest, mask):
                    prefix = bin(
                        struct.unpack("!I", socket.inet_aton(mask))[0]
                    ).count("1")
                    if best is None or prefix > best[0]:
                        best = (prefix, iface)
            except OSError:
                continue
        if best is None:
            return None
        egress = best[1]
        # LAG expansion: management via a port-channel means every member
        # is load-bearing; management via a member implicates its LAG and
        # every sibling member.
        result = {egress}
        for lag, members in self.lag_members.items():
            if egress == lag or egress in members:
                result.add(lag)
                result.update(members)
        return result


@dataclass
class _DomainRecord:
    timestamps: list[float] = field(default_factory=list)
    ports: list[tuple[float, str]] = field(default_factory=list)


class NetworkActionTracker:
    """Fault-domain blast-radius bookkeeping (port -> LAG -> switch)."""

    def __init__(self, window_s: float = DOMAIN_WINDOW_S) -> None:
        self.window_s = window_s
        self._domains: dict[str, _DomainRecord] = {}

    def _prune(self, record: _DomainRecord, now: float) -> None:
        cutoff = now - self.window_s
        record.timestamps = [t for t in record.timestamps if t > cutoff]
        record.ports = [(t, p) for t, p in record.ports if t > cutoff]

    def allows(
        self,
        port: str,
        lag: Optional[str],
        switch_id: str = "self",
        now: Optional[float] = None,
    ) -> tuple[bool, str]:
        """May a disruptive action run on this port now?"""
        now = time.time() if now is None else now
        if lag is not None:
            record = self._domains.setdefault(f"lag:{lag}", _DomainRecord())
            self._prune(record, now)
            other_ports = {p for _, p in record.ports if p != port}
            if other_ports:
                return False, (
                    f"blast radius: LAG {lag} already had a disruptive "
                    f"action on {sorted(other_ports)[0]} within "
                    f"{int(self.window_s)}s — never two ports of one LAG"
                )
        record = self._domains.setdefault(
            f"switch:{switch_id}", _DomainRecord()
        )
        self._prune(record, now)
        if record.timestamps:
            return False, (
                f"blast radius: switch domain already had a disruptive "
                f"action within {int(self.window_s)}s (limit 1)"
            )
        return True, ""

    def record(
        self,
        port: str,
        lag: Optional[str],
        switch_id: str = "self",
        now: Optional[float] = None,
    ) -> None:
        now = time.time() if now is None else now
        if lag is not None:
            rec = self._domains.setdefault(f"lag:{lag}", _DomainRecord())
            rec.ports.append((now, port))
        rec = self._domains.setdefault(f"switch:{switch_id}", _DomainRecord())
        rec.timestamps.append(now)
