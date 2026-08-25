"""R4-1 exit gate (Infrastructure Breadth).

Exit criteria from the R4 Architecture Amendment §3:
  1. Agent configured with ``protocol: ipmi`` passes all existing skill
     tests -- the full agent loop runs against an IPMI backend, the
     shipped skill files produce the same verdicts as on Redfish data.
  2. Cross-site pattern detection identifies batch failures spanning
     2+ sites.

Plus the architecture guarantee: adding a protocol required no changes
to the reasoning, autonomy, skills, playbooks, or fleet-learning layers.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from harkeniq.agent import Agent
from harkeniq.config import load_config, validate_config
from harkeniq.mock.ipmi_sim import MockIPMIBMC
from harkeniq.models import AgentState, VerdictSeverity
from harkeniq.protocols.ipmi import IPMIProtocol, PyghmiBackend

REPO = Path(__file__).parents[2]


async def wait_until(predicate, timeout=5.0, message="condition not met in time"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(message)


def _make_ipmi_agent(bmc: MockIPMIBMC, monkeypatch, **overrides) -> Agent:
    """Agent with bmc.protocol=ipmi, backed by the in-process IPMI mock."""
    from harkeniq import agent as agent_mod

    def fake_create(protocol_name, host, **kwargs):
        assert protocol_name == "ipmi"
        return IPMIProtocol(
            host=host,
            port=kwargs.get("port", 623),
            backend_factory=bmc.factory(),
        )

    monkeypatch.setattr(agent_mod, "create_device_protocol", fake_create)
    config = {
        "bmc": {"host": "10.0.0.9", "protocol": "ipmi",
                "username": "admin", "password": "password"},
        "skills": {"directory": str(REPO / "skills")},
        "polling": {"sensor_interval": 0.05},
    }
    config.update(overrides)
    return Agent(config)


class TestConfigProtocolSelection:
    def test_default_is_redfish(self):
        config = load_config(env={"HARKENIQ_BMC_HOST": "https://bmc"})
        assert config["bmc"]["protocol"] == "redfish"
        assert validate_config(config) == []

    def test_ipmi_via_env(self):
        config = load_config(env={
            "HARKENIQ_BMC_HOST": "10.0.0.9",
            "HARKENIQ_BMC_PROTOCOL": "ipmi",
        })
        assert config["bmc"]["protocol"] == "ipmi"
        assert validate_config(config) == []

    def test_unknown_protocol_rejected(self):
        # "gnmi" became a real protocol in R6; snmp remains unknown.
        config = load_config(env={
            "HARKENIQ_BMC_HOST": "10.0.0.9",
            "HARKENIQ_BMC_PROTOCOL": "snmp",
        })
        errors = validate_config(config)
        assert any("bmc.protocol" in e for e in errors)


class TestIPMIAgentLoop:
    """Exit criterion 1: the full agent loop on protocol=ipmi with the
    shipped skill files."""

    async def test_loop_produces_healthy_verdicts(self, monkeypatch):
        bmc = MockIPMIBMC()
        agent = _make_ipmi_agent(bmc, monkeypatch)
        task = asyncio.create_task(agent.run(install_signal_handlers=False))
        try:
            await wait_until(lambda: agent._last_verdicts,
                             message="no verdicts produced on IPMI")
            assert agent.protocol.name == "ipmi"
            assert agent.client is None  # no Redfish client constructed
            assert agent.device_identity.vendor == "dell"
            severities = {v.severity for v in agent._last_verdicts}
            assert severities == {VerdictSeverity.HEALTHY}
        finally:
            agent.request_shutdown()
            await asyncio.wait_for(task, timeout=5.0)
        assert agent.state_machine.current_state == AgentState.OBSERVING
        assert bmc.closed is True

    async def test_injected_fault_reaches_verdict(self, monkeypatch):
        bmc = MockIPMIBMC()
        agent = _make_ipmi_agent(bmc, monkeypatch)
        task = asyncio.create_task(agent.run(install_signal_handlers=False))
        try:
            await wait_until(lambda: agent._last_verdicts)
            bmc.inject_fault("fan_failure", name="Fan1A")

            def fan_critical():
                return any(
                    v.sensor_id == "fan:Fan1A"
                    and v.severity == VerdictSeverity.CRITICAL
                    for v in agent._last_verdicts
                )

            await wait_until(fan_critical,
                             message="IPMI fan failure did not produce "
                                     "a CRITICAL verdict")
        finally:
            agent.request_shutdown()
            await asyncio.wait_for(task, timeout=5.0)

    async def test_all_five_skill_targets_evaluated(self, monkeypatch):
        bmc = MockIPMIBMC()
        agent = _make_ipmi_agent(bmc, monkeypatch)
        task = asyncio.create_task(agent.run(install_signal_handlers=False))
        try:
            await wait_until(lambda: agent._last_verdicts)
            targets = {v.sensor_id.split(":", 1)[0]
                       for v in agent._last_verdicts}
            assert targets == {"fan", "disk", "memory", "psu", "thermal"}
        finally:
            agent.request_shutdown()
            await asyncio.wait_for(task, timeout=5.0)


class TestPyghmiBackend:
    """The real backend maps the seam onto pyghmi's Command API."""

    def test_pyghmi_importable(self):
        from pyghmi.ipmi.command import Command  # noqa: F401
        from pyghmi.constants import Health
        assert Health.Ok == 0 and Health.Warning == 1

    def test_constructor_and_calls(self):
        with patch("pyghmi.ipmi.command.Command") as mock_cmd_cls:
            mock_cmd = MagicMock()
            mock_cmd_cls.return_value = mock_cmd
            backend = PyghmiBackend("10.0.0.9", 623, "admin", "secret")
            mock_cmd_cls.assert_called_once_with(
                bmc="10.0.0.9", userid="admin", password="secret", port=623,
            )
            mock_cmd.get_sensor_data.return_value = iter([1, 2])
            assert backend.get_sensors() == [1, 2]
            mock_cmd.get_inventory_of_component.return_value = {"Manufacturer": "Dell Inc."}
            assert backend.get_inventory() == {"Manufacturer": "Dell Inc."}
            mock_cmd.get_event_log.return_value = iter([{"record_id": 1}])
            assert backend.get_event_log(clear=True) == [{"record_id": 1}]
            mock_cmd.get_event_log.assert_called_with(clear=True)
            backend.set_identify(on=True, blink=True)
            mock_cmd.set_identify.assert_called_once_with(on=True, blink=True)
            backend.close()
            mock_cmd.ipmi_session.logout.assert_called_once()


class TestCrossSiteExitGate:
    """Exit criterion 2: cross-site batch failures spanning 2+ sites."""

    async def test_two_site_batch_detected_end_to_end(self):
        from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
        from harkeniq_cc.db.models import CCOutcomeHistory
        from harkeniq_cc.db.repos import FleetPatternRepo, SiteRepo
        from harkeniq_cc.intelligence import IntelligenceEngine

        engine = make_engine("sqlite+aiosqlite:///:memory:")
        await create_all(engine)
        sessionmaker = make_sessionmaker(engine)
        async with sessionmaker() as session:
            repo = SiteRepo(session)
            site_a = await repo.upsert("t1", "dc-blr-1", "https://sm1:50051")
            site_b = await repo.upsert("t1", "dc-mum-1", "https://sm2:50051")
            for site in (site_a, site_b):
                for _ in range(3):
                    session.add(CCOutcomeHistory(
                        site_id=site.id, action_id="a", action_type="FAN_RESET",
                        device_agent_id="d", vendor="dell", model="R750",
                        outcome="FAILURE",
                    ))
            await session.commit()

            intel = IntelligenceEngine()
            patterns = await intel.run_cycle(session, "t1")
            await session.commit()

            cross = [p for p in patterns if p.pattern_type == "cross_site_batch"]
            assert len(cross) == 1
            assert cross[0].evidence["sites_affected"] == 2
            stored = await FleetPatternRepo(session).list_patterns(
                pattern_type="cross_site_batch"
            )
            assert len(stored) == 1
        await engine.dispose()


class TestArchitectureGuarantee:
    """Adding IPMI required no changes in protocol-agnostic layers."""

    @pytest.mark.parametrize("module_path", [
        "harkeniq.skills.engine",
        "harkeniq.skills.loader",
        "harkeniq.autonomy.claim",
        "harkeniq.autonomy.quorum",
        "harkeniq.autonomy.suspicion",
        "harkeniq.actions.playbook",
        "harkeniq.actions.queue",
    ])
    def test_no_ipmi_coupling(self, module_path):
        import importlib
        import inspect
        module = importlib.import_module(module_path)
        source = inspect.getsource(module)
        assert "ipmi" not in source.lower()
        assert "pyghmi" not in source.lower()
