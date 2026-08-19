"""Integration path 21: peer heartbeat mesh (Doc 12 §3.1, Doc 06 §9).

Two in-process agents, each polling its own mock simulator, exchange
real UDP heartbeats on loopback with compressed intervals (0.2s beat,
3x timeout = 0.6s — the scaled analogue of the 10s/30s production
schedule). Agent A must mark agent B UNRESPONSIVE within the scaled
detection bound and retain B's pre-failure health evidence.
"""

import asyncio
import time
from pathlib import Path

import pytest

from harkeniq.agent import Agent
from harkeniq.mock.simulator import MockSimulator
from harkeniq.models import PeerStatus

REPO = Path(__file__).parents[2]

BEAT = 0.2  # compressed heartbeat.interval
TIMEOUT = BEAT * 3  # timeout_multiplier 3 -> 0.6s


def agent_config(sim, name, my_port, peer_port):
    return {
        "agent": {"id": f"agent-{name}", "name": f"rack-12-srv-{name}"},
        "bmc": {"host": sim.url, "username": "admin", "password": "password",
                "verify_ssl": False},
        "skills": {"directory": str(REPO / "skills")},
        "polling": {"sensor_interval": 0.5},
        "heartbeat": {"port": my_port, "interval": BEAT, "timeout_multiplier": 3,
                      "secret": "site-secret"},
        "peers": [{"host": "127.0.0.1", "port": peer_port}],
    }


async def wait_until(predicate, timeout=8.0, message="condition not met in time"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(message)


@pytest.fixture
async def two_sims():
    sims = [MockSimulator(device="dell-r750", port=0, no_auth=True),
            MockSimulator(device="hpe-dl360-gen10", port=0, no_auth=True)]
    for sim in sims:
        await sim.start()
    yield sims
    for sim in sims:
        await sim.stop()


class TestPath21PeerHeartbeat:
    async def test_mutual_alive_then_death_detected(
        self, two_sims, unused_udp_port_factory
    ):
        sim_a, sim_b = two_sims
        port_a, port_b = unused_udp_port_factory(), unused_udp_port_factory()
        agent_a = Agent(agent_config(sim_a, "a", port_a, peer_port=port_b))
        agent_b = Agent(agent_config(sim_b, "b", port_b, peer_port=port_a))

        task_a = asyncio.create_task(agent_a.run(install_signal_handlers=False))
        task_b = asyncio.create_task(agent_b.run(install_signal_handlers=False))
        try:
            # Both agents see each other ALIVE
            await wait_until(
                lambda: agent_a.tracker
                and agent_a.tracker.get_peer("127.0.0.1")
                and agent_a.tracker.get_peer("127.0.0.1").status == PeerStatus.ALIVE
                and agent_b.tracker.get_peer("127.0.0.1").status == PeerStatus.ALIVE,
                message="agents never saw each other alive",
            )

            # Identity + health flow over authenticated heartbeats
            b_view = agent_a.tracker.get_peer("127.0.0.1")
            assert b_view.peer_id == "agent-b"
            assert b_view.name == "rack-12-srv-b"
            await wait_until(lambda: b_view.last_known_health,
                             message="no health summary received")
            assert set(b_view.last_known_health) == {
                "fan", "disk", "memory", "psu", "thermal"
            }

            # Kill agent B; A must detect within the scaled 30s-analogue bound
            agent_b.request_shutdown()
            await asyncio.wait_for(task_b, timeout=5.0)
            died_at = time.monotonic()

            await wait_until(
                lambda: b_view.status == PeerStatus.UNRESPONSIVE,
                message="peer death not detected",
            )
            elapsed = time.monotonic() - died_at
            # detection needs at least the 3-missed-beats timeout, and must
            # land well within the wall-clock bound (30s spec -> scaled)
            assert TIMEOUT * 0.5 < elapsed < TIMEOUT * 6, f"detected after {elapsed:.2f}s"

            # Pre-failure witness evidence retained (Doc 06 §9.4)
            assert b_view.health_buffer, "pre-failure health buffer empty"
            assert b_view.last_known_health
            assert b_view.last_heartbeat  # timestamp of the final beat
        finally:
            agent_a.request_shutdown()
            agent_b.request_shutdown()
            for task in (task_a, task_b):
                if not task.done():
                    await asyncio.wait_for(task, timeout=5.0)

    async def test_wrong_secret_rejected(self, two_sims, unused_udp_port_factory):
        """A peer with a mismatched site secret never becomes ALIVE."""
        sim_a, sim_b = two_sims
        port_a, port_b = unused_udp_port_factory(), unused_udp_port_factory()
        cfg_b = agent_config(sim_b, "b", port_b, peer_port=port_a)
        cfg_b["heartbeat"]["secret"] = "wrong-secret"
        agent_a = Agent(agent_config(sim_a, "a", port_a, peer_port=port_b))
        agent_b = Agent(cfg_b)

        task_a = asyncio.create_task(agent_a.run(install_signal_handlers=False))
        task_b = asyncio.create_task(agent_b.run(install_signal_handlers=False))
        try:
            # give several beat intervals for (rejected) traffic to flow
            await asyncio.sleep(BEAT * 5)
            assert agent_a.tracker.get_peer("127.0.0.1").status == PeerStatus.UNKNOWN
        finally:
            agent_a.request_shutdown()
            agent_b.request_shutdown()
            for task in (task_a, task_b):
                if not task.done():
                    await asyncio.wait_for(task, timeout=5.0)
