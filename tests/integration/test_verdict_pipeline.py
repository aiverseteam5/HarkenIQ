"""Integration tests: full agent pipeline (Doc 12 §3.1).

Covers fault recovery paths 11-20 (5 fault types x 2 vendors), the
trending prediction path 22, plus checkpoint-restart and action-proposal
wiring. Peer heartbeat (path 21) is deferred to Phase 3.

Each test: start mock simulator -> Agent.start() -> poll_and_evaluate()
cycles -> verify debounced verdict transitions.
"""

import time
from pathlib import Path

import pytest

from harkeniq.agent import Agent
from harkeniq.mock.simulator import MockSimulator
from harkeniq.models import ActionType, AgentState, VerdictSeverity

REPO = Path(__file__).parents[2]

H = VerdictSeverity.HEALTHY
W = VerdictSeverity.WARNING
C = VerdictSeverity.CRITICAL
T = VerdictSeverity.TRENDING


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
async def dell_sim():
    sim = MockSimulator(device="dell-r750", port=0, no_auth=True)
    await sim.start()
    yield sim
    await sim.stop()


@pytest.fixture
async def hpe_sim():
    sim = MockSimulator(device="hpe-dl360-gen10", port=0, no_auth=True)
    await sim.start()
    yield sim
    await sim.stop()


def make_config(sim, **overrides):
    config = {
        "bmc": {
            "host": sim.url,
            "username": "admin",
            "password": "password",
            "verify_ssl": False,
        },
        "skills": {"directory": str(REPO / "skills")},
    }
    config.update(overrides)
    return config


async def make_agent(sim, **overrides) -> Agent:
    agent = Agent(make_config(sim, **overrides))
    await agent.start()
    return agent


def worst(verdicts, target: str) -> VerdictSeverity:
    """Worst non-TRENDING severity across a subsystem's verdicts."""
    severities = [
        v.severity for v in verdicts
        if v.sensor_id.startswith(f"{target}:") and v.severity != T
    ]
    assert severities, f"no {target} verdicts produced"
    return max(severities)


async def assert_recovery(
    agent,
    sim,
    fault_type: str,
    fault_target: str,
    fault_params: dict,
    expected: VerdictSeverity,
    confirm_polls: int,
):
    """Generic detect-then-recover cycle with default debounce.

    Detection: CRITICAL 2-of-3 consecutive, WARNING 3-of-5.
    Recovery: 3 consecutive HEALTHY (Doc 06 §5.1).
    """
    target = fault_type

    # Healthy baseline poll
    assert worst(await agent.poll_and_evaluate(), target) == H

    # Inject fault; debounce holds until confirm_polls raw observations
    await sim.inject_fault(fault_type, fault_target, fault_params)
    for _ in range(confirm_polls - 1):
        await agent.poll_and_evaluate()
    assert worst(await agent.poll_and_evaluate(), target) == expected

    # Clear the fault; recovery needs 3 consecutive HEALTHY polls
    await sim.reset()
    assert worst(await agent.poll_and_evaluate(), target) == expected  # 1/3
    assert worst(await agent.poll_and_evaluate(), target) == expected  # 2/3
    assert worst(await agent.poll_and_evaluate(), target) == H  # 3/3 -> recovered


# ---------------------------------------------------------------------------
# Fault Recovery Paths 11-20 (Doc 12 §3.1)
# ---------------------------------------------------------------------------


class TestDellRecovery:
    async def test_path_11_fan_recovery(self, dell_sim):
        agent = await make_agent(dell_sim)
        try:
            await assert_recovery(
                agent, dell_sim, "fan", "Fan1A",
                {"health": "Critical", "speed_rpm": 0}, C, confirm_polls=2,
            )
        finally:
            await agent.stop()

    async def test_path_13_disk_recovery(self, dell_sim):
        agent = await make_agent(dell_sim)
        try:
            await assert_recovery(
                agent, dell_sim, "disk", "drive_0",
                {"health": "Warning", "life_left_pct": 18, "smart_alert": True},
                W, confirm_polls=3,
            )
        finally:
            await agent.stop()

    async def test_path_15_memory_recovery(self, dell_sim):
        agent = await make_agent(dell_sim)
        try:
            await assert_recovery(
                agent, dell_sim, "memory", "a1",
                {"health": "Critical", "alarm_ecc_uncorrectable": True},
                C, confirm_polls=2,
            )
        finally:
            await agent.stop()

    async def test_path_17_psu_recovery(self, dell_sim):
        agent = await make_agent(dell_sim)
        try:
            await assert_recovery(
                agent, dell_sim, "psu", "PS2",
                {"state": "Absent", "health": "Critical",
                 "redundancy_health": "Warning"},
                C, confirm_polls=2,
            )
        finally:
            await agent.stop()

    async def test_path_19_thermal_recovery(self, dell_sim):
        agent = await make_agent(dell_sim)
        try:
            await assert_recovery(
                agent, dell_sim, "thermal", "Inlet",
                {"reading_c": 48, "health": "Critical"}, C, confirm_polls=2,
            )
        finally:
            await agent.stop()


class TestHPERecovery:
    async def test_path_12_fan_recovery(self, hpe_sim):
        agent = await make_agent(hpe_sim)
        try:
            await assert_recovery(
                agent, hpe_sim, "fan", "Fan 1",
                {"health": "Critical", "speed_rpm": 0}, C, confirm_polls=2,
            )
        finally:
            await agent.stop()

    async def test_path_14_disk_recovery(self, hpe_sim):
        agent = await make_agent(hpe_sim)
        try:
            await assert_recovery(
                agent, hpe_sim, "disk", "drive_0",
                {"health": "Warning", "life_left_pct": 18, "smart_alert": True},
                W, confirm_polls=3,
            )
        finally:
            await agent.stop()

    async def test_path_16_memory_recovery(self, hpe_sim):
        agent = await make_agent(hpe_sim)
        try:
            await assert_recovery(
                agent, hpe_sim, "memory", "proc1dimm1",
                {"health": "Critical", "alarm_ecc_uncorrectable": True},
                C, confirm_polls=2,
            )
        finally:
            await agent.stop()

    async def test_path_18_psu_recovery(self, hpe_sim):
        agent = await make_agent(hpe_sim)
        try:
            await assert_recovery(
                agent, hpe_sim, "psu", "PS2",
                {"state": "Absent", "health": "Critical",
                 "redundancy_health": "Warning"},
                C, confirm_polls=2,
            )
        finally:
            await agent.stop()

    async def test_path_20_thermal_recovery(self, hpe_sim):
        agent = await make_agent(hpe_sim)
        try:
            await assert_recovery(
                agent, hpe_sim, "thermal", "Inlet",
                {"reading_c": 48, "health": "Critical"}, C, confirm_polls=2,
            )
        finally:
            await agent.stop()


# ---------------------------------------------------------------------------
# Path 22: Trending prediction
# ---------------------------------------------------------------------------

FAST_LEARNING = {
    "baseline": {"min_samples": 8, "window_samples": 40, "critical_pause_samples": 3},
    "trending": {"min_samples": 8, "slope_threshold": 0.05,
                 "r_squared_min": 0.5, "max_projection_days": 90},
}


class TestTrendingPath:
    async def test_path_22_declining_fan_rpm(self, dell_sim):
        """Gradually declining fan RPM -> TRENDING with slope + projection."""
        agent = await make_agent(dell_sim, **FAST_LEARNING)
        t0 = time.time()
        try:
            trending = []
            for i in range(16):
                # -60 RPM per simulated 60s poll = -3600 RPM/hr
                await dell_sim.inject_fault(
                    "fan", "Fan1A", {"speed_rpm": 9000 - 60 * i}
                )
                verdicts = await agent.poll_and_evaluate(timestamp=t0 + i * 60)
                trending = [
                    v for v in verdicts
                    if v.severity == T and "Fan1A" in v.sensor_id
                ]

            assert trending, "expected TRENDING verdict for declining Fan1A"
            verdict = trending[0]
            assert verdict.skill_name == "fan-health"
            assert "RPM/hr" in verdict.message
            fields = verdict.evidence[0].fields
            assert fields["slope"] == pytest.approx(-3600.0, rel=0.05)
            assert fields["r_squared"] > 0.9
            assert fields["threshold_value"] == 480
            assert 0 < fields["time_to_threshold_hours"] < 90 * 24

            # Threshold verdict stays HEALTHY alongside TRENDING (Doc 13 §3.5)
            assert worst(agent._last_verdicts, "fan") == H
        finally:
            await agent.stop()

    async def test_stable_fan_no_trending(self, dell_sim):
        agent = await make_agent(dell_sim, **FAST_LEARNING)
        t0 = time.time()
        try:
            for i in range(12):
                verdicts = await agent.poll_and_evaluate(timestamp=t0 + i * 60)
            assert not [v for v in verdicts if v.severity == T]
        finally:
            await agent.stop()


# ---------------------------------------------------------------------------
# Cross-cutting: state machine, actions, checkpoint restart
# ---------------------------------------------------------------------------


class TestAgentWiring:
    async def test_state_machine_cycle(self, dell_sim):
        agent = await make_agent(dell_sim)
        try:
            assert agent.state_machine.current_state == AgentState.OBSERVING
            await agent.poll_and_evaluate()
            assert agent.state_machine.current_state == AgentState.OBSERVING
            states = [t.to_state for t in agent.state_machine.get_history()]
            assert states == [
                AgentState.OBSERVING,  # BOOTING -> OBSERVING at startup
                AgentState.EVALUATING,
                AgentState.DECIDING,
                AgentState.OBSERVING,
            ]
        finally:
            await agent.stop()

    async def test_action_proposed_on_fan_failure(self, dell_sim):
        """fan-health rule 0 (health Critical) recommends COLLECT_DIAGNOSTICS
        with a 1-of-1 debounce override -> action proposed on first poll."""
        agent = await make_agent(dell_sim, **FAST_LEARNING)
        t0 = time.time()
        try:
            for i in range(9):  # reach confidence 1.0 (expressions active)
                await agent.poll_and_evaluate(timestamp=t0 + i * 60)
            assert agent.get_pending_actions() == []

            await dell_sim.inject_fault(
                "fan", "Fan1A", {"health": "Critical", "speed_rpm": 0}
            )
            await agent.poll_and_evaluate(timestamp=t0 + 9 * 60)
            actions = agent.get_pending_actions()
            assert len(actions) == 1
            action = actions[0]
            assert action.type == ActionType.COLLECT_DIAGNOSTICS
            assert "Fan1A" in action.params["reason"]
            assert "Fan1A" in action.sensor_id
            assert action.verdict_severity == C
            # Action path traversed: DECIDING -> AWAITING_AUTH -> REPORTING
            states = [t.to_state for t in agent.state_machine.get_history()]
            assert AgentState.AWAITING_AUTH in states
            assert AgentState.REPORTING in states
        finally:
            await agent.stop()

    async def test_checkpoint_restart_restores_baselines(self, dell_sim, tmp_path):
        checkpoint = {"path": str(tmp_path / "agent.db"), "interval": 999999}
        t0 = time.time()

        agent1 = await make_agent(dell_sim, checkpoint=checkpoint, **FAST_LEARNING)
        for i in range(10):
            await agent1.poll_and_evaluate(timestamp=t0 + i * 60)
        fan_ids = [
            sid for sid in agent1.skill_engine.trending.get_all_baselines()
            if sid.startswith("fan:")
        ]
        assert fan_ids
        await agent1.stop()  # writes final checkpoint

        agent2 = await make_agent(dell_sim, checkpoint=checkpoint, **FAST_LEARNING)
        try:
            restored = agent2.skill_engine.trending.get_all_baselines()
            assert set(fan_ids) <= set(restored)
            # Confidence carries over: expressions active on the first poll
            assert agent2.skill_engine.trending.confidence(fan_ids[0]) == 1.0
            verdicts = await agent2.poll_and_evaluate(timestamp=t0 + 11 * 60)
            fan_verdict = next(
                v for v in verdicts
                if v.sensor_id == fan_ids[0] and v.severity != T
            )
            # Expression path (not learning pass-through): evidence has no
            # pass-through marker and message is not the learning message
            assert "baseline learning" not in fan_verdict.message
        finally:
            await agent2.stop()
