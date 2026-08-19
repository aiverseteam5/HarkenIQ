"""Integration: action approval + execution end-to-end (Doc 06 §11A).

Fault -> action proposed (PENDING) -> operator approval via the CLI
(out-of-band, through the checkpoint DB) -> next agent cycle traverses
AWAITING_AUTH -> ACTING -> REPORTING -> OBSERVING, the Redfish action
lands on the (mock) BMC, and the audit trail records the full lifecycle.
"""

import asyncio
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from harkeniq.agent import Agent
from harkeniq.cli import main as cli_main
from harkeniq.mock.simulator import MockSimulator
from harkeniq.models import ActionStatus, ActionType, AgentState
from harkeniq.state.checkpoint import CheckpointManager

REPO = Path(__file__).parents[2]

FAST_LEARNING = {
    "baseline": {"min_samples": 8, "window_samples": 40, "critical_pause_samples": 3},
    "trending": {"min_samples": 8, "slope_threshold": 0.05,
                 "r_squared_min": 0.5, "max_projection_days": 90},
}


@pytest.fixture
async def dell_sim():
    sim = MockSimulator(device="dell-r750", port=0, no_auth=True)
    await sim.start()
    yield sim
    await sim.stop()


async def make_agent(sim, tmp_path):
    agent = Agent({
        "bmc": {"host": sim.url, "username": "admin", "password": "password",
                "verify_ssl": False},
        "skills": {"directory": str(REPO / "skills")},
        "checkpoint": {"path": str(tmp_path / "agent.db"), "interval": 999999},
        **FAST_LEARNING,
    })
    await agent.start()
    return agent


async def learn_baseline(agent, t0, polls=9):
    for i in range(polls):
        await agent.poll_and_evaluate(timestamp=t0 + i * 60)


async def invoke_cli(args):
    """Run the (synchronous, asyncio.run-based) CLI off the test's event loop."""
    return await asyncio.to_thread(CliRunner().invoke, cli_main, args)


async def audit_outcomes(db_path):
    cp = CheckpointManager(db_path)
    rows = cp._conn.execute(
        "SELECT action, outcome, authorization FROM audit_log ORDER BY id"
    ).fetchall()
    await cp.close()
    return [(r["action"], r["outcome"], r["authorization"]) for r in rows]


class TestActionExecutionE2E:
    async def test_fan_failure_full_action_lifecycle(self, dell_sim, tmp_path):
        db = str(tmp_path / "agent.db")
        agent = await make_agent(dell_sim, tmp_path)
        t0 = time.time()
        try:
            await learn_baseline(agent, t0)
            assert agent.get_pending_actions() == []

            # Fan failure -> COLLECT_DIAGNOSTICS proposed (1-of-1 debounce)
            await dell_sim.inject_fault(
                "fan", "Fan1A", {"health": "Critical", "speed_rpm": 0}
            )
            await agent.poll_and_evaluate(timestamp=t0 + 9 * 60)
            pending = agent.get_pending_actions()
            assert len(pending) == 1
            action = pending[0]
            assert action.type == ActionType.COLLECT_DIAGNOSTICS

            # Second cycle with the fault still present: dedup, no re-proposal
            await agent.poll_and_evaluate(timestamp=t0 + 10 * 60)
            assert len(agent.get_pending_actions()) == 1

            # Out-of-band operator approval through the CLI (Doc 06 §11A.2)
            result = await invoke_cli(
                ["action", "approve", action.id, "--checkpoint", db, "--by", "vinod"]
            )
            assert result.exit_code == 0, result.output

            # Next cycle picks up the approval and executes
            await agent.poll_and_evaluate(timestamp=t0 + 11 * 60)
            executed = agent.action_queue.get(action.id)
            assert executed.status == ActionStatus.COMPLETED
            assert executed.outcome.success is True
            assert executed.approved_at

            # State machine traversed the acting path
            states = [t.to_state for t in agent.state_machine.get_history()]
            assert AgentState.AWAITING_AUTH in states
            assert AgentState.ACTING in states
            assert AgentState.REPORTING in states
            assert agent.state_machine.current_state == AgentState.OBSERVING

            # The Redfish action landed on the BMC (Dell sysconfig export)
            assert dell_sim.action_state["diagnostics"], "no diagnostics collected"
            assert dell_sim.action_state["diagnostics"][0]["vendor"] == "dell"

            # Queue no longer lists it as pending or approved
            assert agent.get_pending_actions() == []
            assert agent.action_queue.approved() == []
        finally:
            await agent.stop()

        # Full audit lifecycle persisted (Doc 06 §11A.2)
        outcomes = await audit_outcomes(db)
        trail = [o for a, o, _ in outcomes if a == "COLLECT_DIAGNOSTICS"]
        assert trail == ["proposed", "approved", "executing", "success"]
        approved_row = next(r for r in outcomes if r[1] == "approved")
        assert approved_row[2] == "vinod"

    async def test_denied_action_never_executes_or_reproposes(self, dell_sim, tmp_path):
        db = str(tmp_path / "agent.db")
        agent = await make_agent(dell_sim, tmp_path)
        t0 = time.time()
        try:
            await learn_baseline(agent, t0)
            await dell_sim.inject_fault(
                "fan", "Fan1A", {"health": "Critical", "speed_rpm": 0}
            )
            await agent.poll_and_evaluate(timestamp=t0 + 9 * 60)
            action = agent.get_pending_actions()[0]

            result = await invoke_cli(["action", "deny", action.id, "--checkpoint", db])
            assert result.exit_code == 0, result.output

            # Fault persists across further cycles: denial is final (D16)
            for i in range(3):
                await agent.poll_and_evaluate(timestamp=t0 + (10 + i) * 60)
            assert agent.get_pending_actions() == []
            assert agent.action_queue.get(action.id).status == ActionStatus.DENIED
            assert dell_sim.action_state["diagnostics"] == []

            states = [t.to_state for t in agent.state_machine.get_history()]
            assert AgentState.ACTING not in states
        finally:
            await agent.stop()

        outcomes = await audit_outcomes(db)
        trail = [o for a, o, _ in outcomes if a == "COLLECT_DIAGNOSTICS"]
        assert trail == ["proposed", "denied"]

    async def test_disk_failure_identify_led_lands_on_drive(self, dell_sim, tmp_path):
        db = str(tmp_path / "agent.db")
        agent = await make_agent(dell_sim, tmp_path)
        t0 = time.time()
        try:
            await learn_baseline(agent, t0)
            # Failed drive -> IDENTIFY_LED with target = drive name
            await dell_sim.inject_fault("disk", "drive_0", {"health": "Critical"})
            # disk-health critical debounce is 2-of-3
            await agent.poll_and_evaluate(timestamp=t0 + 9 * 60)
            await agent.poll_and_evaluate(timestamp=t0 + 10 * 60)
            pending = agent.get_pending_actions()
            assert len(pending) == 1
            action = pending[0]
            assert action.type == ActionType.IDENTIFY_LED
            # Dell normalization uses the drive Model as its name (Doc 08)
            assert action.params["target"] == "SAMSUNG MZ7LH960HAJR-00005"

            result = await invoke_cli(
                ["action", "approve", action.id, "--checkpoint", db]
            )
            assert result.exit_code == 0, result.output

            await agent.poll_and_evaluate(timestamp=t0 + 11 * 60)
            executed = agent.action_queue.get(action.id)
            assert executed.status == ActionStatus.COMPLETED, executed.outcome
            # LED blinking on the right drive, verified via the simulator
            assert dell_sim.action_state["led"]["Solid State Disk 0:1:0"] == "Blinking"
        finally:
            await agent.stop()
