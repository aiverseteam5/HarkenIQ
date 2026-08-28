"""QA-025: the A2.5 resource monitor is constructed and consulted.

ResourceMonitor existed (and was tested) since R3a but was imported by
nothing in production — HARKENIQ_RESOURCES_PROFILE was silently discarded.
"""

import logging
import time

import pytest

from harkeniq.agent import Agent
from harkeniq.autonomy.resources import (
    PROFILES,
    DegradationLevel,
    ResourceSnapshot,
)
from harkeniq.config import load_config


def make_agent(profile: str | None = None) -> Agent:
    env = {"HARKENIQ_BMC_HOST": "https://127.0.0.1:9"}
    if profile:
        env["HARKENIQ_RESOURCES_PROFILE"] = profile
    config = load_config(env=env)
    return Agent(config)


class TestResourceWiring:
    def test_default_profile_is_standard(self):
        agent = make_agent()
        assert agent.resource_monitor is not None
        assert agent.resource_monitor.profile.name == "standard"

    def test_env_profile_override_finally_works(self):
        # The compose file set this env var for months; it did nothing.
        agent = make_agent("constrained")
        assert agent.resource_monitor.profile.name == "constrained"
        assert agent.resource_monitor.profile.memory_hard_mb == 50

    def test_multiplier_defaults_to_normal(self):
        agent = make_agent()
        assert agent.resource_monitor.poll_interval_multiplier == 1.0


class _StubMonitor:
    """Drives _resource_loop through a scripted sequence of levels."""

    def __init__(self, levels):
        self.profile = PROFILES["standard"]
        self._levels = list(levels)
        self.calls = 0

    def measure(self):
        self.calls += 1
        return ResourceSnapshot(
            memory_rss_mb=42.5, cpu_percent=3.25, timestamp=time.time()
        )

    def evaluate(self, snapshot):
        return self._levels[min(self.calls - 1, len(self._levels) - 1)]


async def drive_resource_loop(agent, levels, iterations):
    """Run _resource_loop for exactly `iterations` passes, then shut down."""
    agent.resource_monitor = _StubMonitor(levels)
    seen = {"n": 0}

    async def fake_pause(seconds):
        seen["n"] += 1
        return seen["n"] >= iterations

    agent._pause = fake_pause
    await agent._resource_loop()
    return agent.resource_monitor


class TestResourceLoopReporting:
    """The loop formats the snapshot it is given. It read snapshot.rss_mb /
    snapshot.cpu_pct while ResourceSnapshot defines memory_rss_mb /
    cpu_percent, so every level change raised AttributeError, got swallowed
    by the bare except, and left last_level pinned at None — the branch
    re-entered and re-threw every check_interval, forever. The ceiling
    ladder still ran; the log line that reports crossing one never could.
    TODOS M7 calls for ceilings "enforced and observable"; observable was
    dead. Nothing exercised this loop, so 2311 tests stayed green.
    """

    @pytest.mark.asyncio
    async def test_sampling_never_fails(self, caplog):
        agent = make_agent()
        with caplog.at_level(logging.INFO, logger="harkeniq.agent"):
            await drive_resource_loop(agent, [DegradationLevel.NORMAL], 1)
        assert "Resource sampling failed" not in caplog.text

    @pytest.mark.asyncio
    async def test_first_sample_reports_profile_and_rss(self, caplog):
        agent = make_agent()
        with caplog.at_level(logging.INFO, logger="harkeniq.agent"):
            await drive_resource_loop(agent, [DegradationLevel.NORMAL], 1)
        assert "Resource monitor active: profile=standard rss=42.5MB" in caplog.text

    @pytest.mark.asyncio
    async def test_crossing_a_ceiling_is_reported(self, caplog):
        agent = make_agent()
        with caplog.at_level(logging.INFO, logger="harkeniq.agent"):
            await drive_resource_loop(
                agent,
                [DegradationLevel.NORMAL, DegradationLevel.THROTTLED],
                2,
            )
        assert (
            "Resource level THROTTLED (rss=42.5MB cpu=3.2%, profile=standard)"
            in caplog.text
        )

    @pytest.mark.asyncio
    async def test_steady_level_reports_once_not_every_interval(self, caplog):
        # Pins last_level advancing. Under the bug this logged on every pass.
        agent = make_agent()
        with caplog.at_level(logging.INFO, logger="harkeniq.agent"):
            await drive_resource_loop(agent, [DegradationLevel.NORMAL], 4)
        assert caplog.text.count("Resource monitor active") == 1
