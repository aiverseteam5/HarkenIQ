"""Continuous run loop + signal handling tests (Doc 06 §2, §14, §15)."""

import asyncio
import logging
import os
import signal
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from harkeniq.agent import POLL_FAILURE_ERROR_THRESHOLD, Agent
from harkeniq.cli import main as cli_main
from harkeniq.errors import RedfishError
from harkeniq.mock.simulator import MockSimulator
from harkeniq.models import AgentState
from harkeniq.state.checkpoint import CheckpointManager

REPO = Path(__file__).parents[2]


@pytest.fixture
async def dell_sim():
    sim = MockSimulator(device="dell-r750", port=0, no_auth=True)
    await sim.start()
    yield sim
    await sim.stop()


def make_config(sim, tmp_path=None, **overrides):
    config = {
        "bmc": {"host": sim.url, "username": "admin", "password": "password",
                "verify_ssl": False},
        "skills": {"directory": str(REPO / "skills")},
        "polling": {"sensor_interval": 0.05},
    }
    if tmp_path is not None:
        config["checkpoint"] = {"path": str(tmp_path / "checkpoint.db"),
                                "interval": 0.01}
    config.update(overrides)
    return config


async def wait_until(predicate, timeout=5.0, message="condition not met in time"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(message)


class TestRunLoop:
    async def test_run_produces_verdicts_and_stops_gracefully(self, dell_sim, tmp_path):
        agent = Agent(make_config(dell_sim, tmp_path))
        task = asyncio.create_task(agent.run(install_signal_handlers=False))
        await wait_until(lambda: agent._last_verdicts, message="no verdicts produced")

        agent.request_shutdown()
        await asyncio.wait_for(task, timeout=5.0)

        # fully stopped: connections closed, back in OBSERVING
        assert agent.client is None
        assert agent.checkpoint is None
        assert agent.state_machine.current_state == AgentState.OBSERVING

        # final checkpoint was written
        cp = CheckpointManager(tmp_path / "checkpoint.db")
        state = await cp.load_checkpoint()
        await cp.close()
        assert state["agent_meta"]["state"] == "OBSERVING"
        assert state["sensor_readings"]
        assert state["baselines"]

    async def test_poll_failure_does_not_kill_loop(self, dell_sim, caplog):
        agent = Agent(make_config(dell_sim))
        await agent.start()

        async def boom():
            raise RedfishError("BMC unreachable")

        agent.poller.poll_sensors = boom
        with caplog.at_level(logging.WARNING, logger="harkeniq.agent"):
            task = asyncio.create_task(agent.run(install_signal_handlers=False))
            await wait_until(
                lambda: agent._poll_failures >= POLL_FAILURE_ERROR_THRESHOLD,
                message="poll failures not accumulated",
            )
            assert not task.done()  # TaskGroup survived the failures
            # state recovered to OBSERVING after each failed cycle
            assert agent.state_machine.current_state == AgentState.OBSERVING
            agent.request_shutdown()
            await asyncio.wait_for(task, timeout=5.0)

        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert any("consecutive" in r.getMessage() for r in errors)

    async def test_poll_success_resets_failure_counter(self, dell_sim):
        agent = Agent(make_config(dell_sim))
        await agent.start()
        real_poll = agent.poller.poll_sensors
        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            if calls["n"] <= 2:
                raise RedfishError("transient")
            return await real_poll()

        agent.poller.poll_sensors = flaky
        task = asyncio.create_task(agent.run(install_signal_handlers=False))
        await wait_until(lambda: calls["n"] >= 3 and agent._poll_failures == 0)
        agent.request_shutdown()
        await asyncio.wait_for(task, timeout=5.0)

    async def test_sigterm_triggers_graceful_shutdown(self, dell_sim):
        agent = Agent(make_config(dell_sim))
        task = asyncio.create_task(agent.run(install_signal_handlers=True))
        await wait_until(lambda: agent._last_verdicts)
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(task, timeout=5.0)
        assert agent.client is None


class TestSighup:
    async def test_sighup_reloads_skills(self, dell_sim):
        agent = Agent(make_config(dell_sim))
        await agent.start()
        before = agent.skill_engine._skills
        agent._on_sighup()
        assert agent.skill_engine._skills is not before  # fresh objects loaded
        assert set(agent.skill_engine._skills) == set(before)
        await agent.stop()

    async def test_sighup_keeps_old_skills_on_error(self, dell_sim, caplog):
        agent = Agent(make_config(dell_sim))
        await agent.start()
        before = agent.skill_engine._skills
        agent.skills_dir = "/nonexistent/skills"
        with caplog.at_level(logging.ERROR, logger="harkeniq.agent"):
            agent._on_sighup()  # must not raise
        assert agent.skill_engine._skills is before
        assert any("reload failed" in r.getMessage() for r in caplog.records)
        await agent.stop()


class TestHealthSummary:
    async def test_all_ok_before_faults(self, dell_sim):
        agent = Agent(make_config(dell_sim))
        await agent.start()
        await agent.poll_and_evaluate()
        summary = agent.health_summary()
        assert set(summary) == {"fan", "disk", "memory", "psu", "thermal"}
        assert all(v == "OK" for v in summary.values())
        await agent.stop()

    async def test_worst_severity_wins(self, dell_sim):
        agent = Agent(make_config(dell_sim))
        await agent.start()
        await dell_sim.inject_fault("fan", "Fan1A", {"health": "Critical", "speed_rpm": 0})
        for _ in range(3):  # debounce critical 2/3
            await agent.poll_and_evaluate()
        summary = agent.health_summary()
        assert summary["fan"] == "CRITICAL"
        assert summary["disk"] == "OK"
        await agent.stop()


class TestAgentCli:
    def _seed_checkpoint(self, tmp_path):
        db = str(tmp_path / "checkpoint.db")

        async def _save():
            cp = CheckpointManager(db)
            await cp.save_checkpoint(
                sensor_readings={}, baselines={}, verdicts=[], peers=[],
                agent_meta={"agent_id": "agent-test", "state": "OBSERVING"},
                log_cursors={},
            )
            await cp.close()

        asyncio.run(_save())
        return db

    def test_agent_status(self, tmp_path):
        db = self._seed_checkpoint(tmp_path)
        result = CliRunner().invoke(cli_main, ["agent", "status", "--checkpoint", db])
        assert result.exit_code == 0, result.output
        assert "agent-test" in result.output
        assert "OBSERVING" in result.output

    def test_agent_checkpoint_summary(self, tmp_path):
        db = self._seed_checkpoint(tmp_path)
        result = CliRunner().invoke(cli_main, ["agent", "checkpoint", "--checkpoint", db])
        assert result.exit_code == 0, result.output
        assert "audit_log" in result.output
        assert "Last written" in result.output

    def test_agent_stop_without_pidfile_fails(self, tmp_path):
        result = CliRunner().invoke(
            cli_main, ["agent", "stop", "--pidfile", str(tmp_path / "nope.pid")]
        )
        assert result.exit_code != 0
        assert "not running" in result.output

    def test_agent_stop_stale_pidfile(self, tmp_path):
        pidfile = tmp_path / "agent.pid"
        pidfile.write_text("999999")  # unlikely to exist
        result = CliRunner().invoke(
            cli_main, ["agent", "stop", "--pidfile", str(pidfile)]
        )
        assert result.exit_code != 0
        assert not pidfile.exists()  # stale pidfile cleaned up

    def test_agent_start_invalid_config_exits_3(self, tmp_path):
        result = CliRunner().invoke(
            cli_main,
            ["agent", "start", "--pidfile", str(tmp_path / "agent.pid")],
            env={"HARKENIQ_BMC_HOST": ""},
        )
        assert result.exit_code == 3
        assert "bmc.host" in result.output
