"""Auto-discovery helpers (A1.1).

Rack hints from agent names are suggestions only — never authoritative;
the operator confirms via dashboard or YAML. Peer adjacency merges the
registration-time peer list with live heartbeat peer_status keys.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

_RACK_RE = re.compile(r"\b(rack[-_]?\d+)", re.IGNORECASE)


def rack_hint(agent_name: str) -> Optional[str]:
    """``rack-12-srv-04`` → ``rack-12``; None when no hint present."""
    match = _RACK_RE.search(agent_name or "")
    if not match:
        return None
    return match.group(1).lower().replace("_", "-")


def peer_graph(devices: Iterable, statuses: dict[str, object]) -> dict[str, set[str]]:
    """Undirected adjacency device_id -> {device_id}.

    Peer keys (registration ``host:port`` strings or heartbeat
    peer_status ids) resolve against agent_id, then agent_name;
    unresolvable keys are dropped — we only correlate what we can name.
    """
    devices = list(devices)
    by_key: dict[str, str] = {}
    for device in devices:
        if device.agent_id:
            by_key[device.agent_id] = device.id
        if device.agent_name:
            by_key.setdefault(device.agent_name, device.id)

    graph: dict[str, set[str]] = {device.id: set() for device in devices}

    def link(a: str, b: str) -> None:
        if a != b:
            graph[a].add(b)
            graph[b].add(a)

    for device in devices:
        for key in device.peers or []:
            peer = by_key.get(key)
            if peer:
                link(device.id, peer)
        status = statuses.get(device.id)
        peer_status = getattr(status, "last_peer_status", None) or {}
        for key in peer_status:
            peer = by_key.get(key)
            if peer:
                link(device.id, peer)
    return graph


def components(graph: dict[str, set[str]]) -> list[set[str]]:
    """Connected components with at least one edge."""
    seen: set[str] = set()
    result: list[set[str]] = []
    for start in graph:
        if start in seen or not graph[start]:
            continue
        component = set()
        stack = [start]
        while stack:
            node = stack.pop()
            if node in component:
                continue
            component.add(node)
            stack.extend(graph[node] - component)
        seen |= component
        result.append(component)
    return result
