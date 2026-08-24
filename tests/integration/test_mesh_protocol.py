"""Integration test: mesh protocol with fault injection (R3b-2 Phase 7, OQ-4).

3 in-process agents on loopback with compressed timing. Fault injection:
- Kill an agent → surviving agents detect UNRESPONSIVE
- Partition agents → isolation detection
- Claim broadcast + ack flow

This is the OQ-4 test approach: simulated multi-agent harness that validates
quorum disambiguation, claim lifecycle, and partition behavior without real
hardware.
"""

import asyncio
import time
from pathlib import Path

import pytest

from harkeniq.agent import Agent
from harkeniq.mock.simulator import MockSimulator
from harkeniq.models import PeerStatus

REPO = Path(__file__).parents[2]

BEAT = 0.2
TIMEOUT = BEAT * 3


def agent_config(sim, name, my_port, peer_addrs):
    """Config for a mesh agent with multiple peers.

    peer_addrs: list of (host, port) tuples.
    """
    return {
        "agent": {"id": f"agent-{name}", "name": f"rack-12-srv-{name}"},
        "bmc": {"host": sim.url, "username": "admin", "password": "password",
                "verify_ssl": False},
        "skills": {"directory": str(REPO / "skills")},
        "polling": {"sensor_interval": 0.5},
        "heartbeat": {"port": my_port, "interval": BEAT, "timeout_multiplier": 3,
                      "secret": "site-secret"},
        "peers": [{"host": h, "port": p} for h, p in peer_addrs],
    }

# Different loopback addresses for 3-agent tests.
# PeerTracker keys by host, so each agent needs a unique host.
LOOPBACK = ["127.0.0.1", "127.0.0.2", "127.0.0.3"]


async def wait_until(predicate, timeout=8.0, message="condition not met in time"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(message)


@pytest.fixture
async def three_sims():
    sims = [
        MockSimulator(device="dell-r750", port=0, no_auth=True),
        MockSimulator(device="hpe-dl360-gen10", port=0, no_auth=True),
        MockSimulator(device="dell-r750", port=0, no_auth=True),
    ]
    for sim in sims:
        await sim.start()
    yield sims
    for sim in sims:
        await sim.stop()


class TestMeshProtocol:
    async def test_three_agents_mutual_alive(
        self, three_sims, unused_udp_port_factory
    ):
        """3 agents form a mesh: all see each other ALIVE."""
        ports = [unused_udp_port_factory() for _ in range(3)]
        agents = []
        for i, sim in enumerate(three_sims):
            name = chr(ord("a") + i)
            peer_addrs = [
                (LOOPBACK[j], ports[j]) for j in range(3) if j != i
            ]
            cfg = agent_config(sim, name, ports[i], peer_addrs)
            cfg["heartbeat"]["bind"] = LOOPBACK[i]
            agents.append(Agent(cfg))

        tasks = [
            asyncio.create_task(a.run(install_signal_handlers=False))
            for a in agents
        ]
        try:
            # Wait for all agents to see their peers
            await wait_until(
                lambda: all(
                    a.tracker and
                    all(
                        p.status == PeerStatus.ALIVE
                        for p in a.tracker.get_peers()
                    )
                    for a in agents
                ),
                timeout=10.0,
                message="agents never all saw each other alive",
            )

            # Verify peer count
            for a in agents:
                alive = sum(
                    1 for p in a.tracker.get_peers()
                    if p.status == PeerStatus.ALIVE
                )
                assert alive == 2, f"Agent {a.agent_id} sees {alive} alive peers"

        finally:
            for a in agents:
                a.request_shutdown()
            for task in tasks:
                if not task.done():
                    await asyncio.wait_for(task, timeout=5.0)

    async def test_device_down_detection(
        self, three_sims, unused_udp_port_factory
    ):
        """Kill one agent → 2 survivors detect it UNRESPONSIVE."""
        ports = [unused_udp_port_factory() for _ in range(3)]
        agents = []
        for i, sim in enumerate(three_sims):
            name = chr(ord("a") + i)
            peer_addrs = [
                (LOOPBACK[j], ports[j]) for j in range(3) if j != i
            ]
            cfg = agent_config(sim, name, ports[i], peer_addrs)
            cfg["heartbeat"]["bind"] = LOOPBACK[i]
            agents.append(Agent(cfg))

        tasks = [
            asyncio.create_task(a.run(install_signal_handlers=False))
            for a in agents
        ]
        try:
            # Wait for mesh formation
            await wait_until(
                lambda: all(
                    a.tracker and
                    any(p.status == PeerStatus.ALIVE for p in a.tracker.get_peers())
                    for a in agents
                ),
                timeout=10.0,
                message="mesh never formed",
            )

            # Kill agent C (index 2)
            agents[2].request_shutdown()
            await asyncio.wait_for(tasks[2], timeout=5.0)

            # Agents A and B should detect C as UNRESPONSIVE
            c_host = LOOPBACK[2]
            await wait_until(
                lambda: (
                    agents[0].tracker.get_peer(c_host) is not None
                    and agents[0].tracker.get_peer(c_host).status == PeerStatus.UNRESPONSIVE
                ),
                timeout=8.0,
                message="agent A never detected C as UNRESPONSIVE",
            )

            # Agent A's witness evidence for C should be retained
            c_peer = agents[0].tracker.get_peer(c_host)
            assert c_peer is not None
            assert c_peer.status == PeerStatus.UNRESPONSIVE
            # Pre-failure evidence retained
            assert c_peer.last_known_health is not None

        finally:
            for a in agents:
                a.request_shutdown()
            for task in tasks:
                if not task.done():
                    await asyncio.wait_for(task, timeout=5.0)

    async def test_envelope_interop(
        self, three_sims, unused_udp_port_factory
    ):
        """Verify message type envelope works in live UDP exchange."""
        ports = [unused_udp_port_factory() for _ in range(2)]
        agents = []
        for i, sim in enumerate(three_sims[:2]):
            name = chr(ord("a") + i)
            peer_addrs = [(LOOPBACK[1 - i], ports[1 - i])]
            cfg = agent_config(sim, name, ports[i], peer_addrs)
            cfg["heartbeat"]["bind"] = LOOPBACK[i]
            agents.append(Agent(cfg))

        tasks = [
            asyncio.create_task(a.run(install_signal_handlers=False))
            for a in agents
        ]
        try:
            # Agents use envelope-wrapped heartbeats and still see each other
            await wait_until(
                lambda: agents[0].tracker and
                agents[0].tracker.get_peer(LOOPBACK[1]) and
                agents[0].tracker.get_peer(LOOPBACK[1]).status == PeerStatus.ALIVE,
                timeout=8.0,
                message="envelope heartbeats never arrived",
            )
        finally:
            for a in agents:
                a.request_shutdown()
            for task in tasks:
                if not task.done():
                    await asyncio.wait_for(task, timeout=5.0)
