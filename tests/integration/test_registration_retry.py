"""QA-041: SM registration is retried by the report loop, not fire-once.

Before this fix a failed startup RegisterAgent logged "continuing
standalone" and never re-attempted (the only other caller was the
firmware-inventory change path) — a transient SM/DB hiccup at boot left
the agent lease-less until restart.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from harkeniq.agent import Agent
from harkeniq.mock.simulator import MockSimulator

REPO = Path(__file__).parents[2]


class FlakyReporter:
    """Reporter stub whose registration fails N times, then succeeds."""

    enabled = True
    agent_id = ""

    def __init__(self, fail_first: int):
        self.fail_first = fail_first
        self.attempts = 0

    async def register_agent(self, **kwargs):
        self.attempts += 1
        if self.attempts <= self.fail_first:
            return None
        return SimpleNamespace(
            sm_public_key_pem=b"", agent_certificate=b"",
            peer_keys={}, peer_keys_signature=b"",
        )

    async def send_heartbeat(self, *args, **kwargs):
        return None

    async def report_verdict(self, verdict):
        return True

    async def report_action(self, action):
        return True

    async def poll_decisions(self):
        return []

    async def poll_directives(self):
        return []

    async def close(self):
        pass


@pytest.fixture
async def agent():
    sim = MockSimulator(device="dell-r750", port=0, no_auth=True)
    await sim.start()
    a = Agent({
        "bmc": {"host": sim.url, "username": "admin", "password": "password",
                "verify_ssl": False},
        "skills": {"directory": str(REPO / "skills")},
        "site_manager": {"heartbeat_interval": 0.05,
                         "action_poll_interval": 0.05},
    })
    await a.start()
    yield a
    await a.stop()
    await sim.stop()


async def test_register_with_sm_sets_flag_only_on_success(agent):
    reporter = FlakyReporter(fail_first=2)
    agent.reporter = reporter

    assert not await agent._register_with_sm()
    assert not agent._sm_registered
    assert not await agent._register_with_sm()
    assert not agent._sm_registered
    assert await agent._register_with_sm()
    assert agent._sm_registered


async def test_report_loop_recovers_registration(agent):
    reporter = FlakyReporter(fail_first=1)
    agent.reporter = reporter
    agent._sm_registered = False

    task = asyncio.create_task(agent._report_loop())
    try:
        async with asyncio.timeout(5):
            while not agent._sm_registered:
                await asyncio.sleep(0.02)
    finally:
        agent._shutdown.set()
        await task
        agent._shutdown.clear()

    assert reporter.attempts >= 2  # first failed, retry landed


async def test_report_loop_stops_retrying_once_registered(agent):
    reporter = FlakyReporter(fail_first=0)
    agent.reporter = reporter
    agent._sm_registered = False

    task = asyncio.create_task(agent._report_loop())
    try:
        async with asyncio.timeout(5):
            while not agent._sm_registered:
                await asyncio.sleep(0.02)
        await asyncio.sleep(0.3)  # several further loop cycles
    finally:
        agent._shutdown.set()
        await task
        agent._shutdown.clear()

    assert reporter.attempts == 1  # no re-registration spam after success