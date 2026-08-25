"""Agent-side directed-directive execution (R5 transport).

The agent polls the SM for directives, executes them through its OWN
executor (allow list, audit -- delivery is not a policy bypass), and
settles each with ReportDirectiveResult.
"""

from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path

import grpc
import pytest

from harkeniq.agent import Agent
from harkeniq.mock.simulator import MockSimulator
from harkeniq.proto import harkeniq_pb2, harkeniq_pb2_grpc

REPO = Path(__file__).parents[2]

COMMUNITY_SKILL = """\
name: directed-fan-watch
version: 1
target: fan
description: Installed via directive
rules:
  - condition: "health == 'Warning'"
    verdict: WARNING
    message: "Fan {name} degraded (directed skill)"
default_verdict: HEALTHY
"""


class FakeDirectiveSM(harkeniq_pb2_grpc.AgentServiceServicer):
    """Serves queued directives once; records settlements."""

    def __init__(self):
        self.directives: list = []
        self.results: list = []

    async def RegisterAgent(self, request, context):
        return harkeniq_pb2.RegistrationAck(accepted=True, site_name="site-1")

    async def Heartbeat(self, request, context):
        return harkeniq_pb2.HeartbeatAck(accepted=True)

    async def ReportVerdict(self, request, context):
        return harkeniq_pb2.VerdictAck(accepted=True)

    async def ReportAction(self, request, context):
        return harkeniq_pb2.ActionAck(accepted=True)

    async def PollActionDecisions(self, request, context):
        return harkeniq_pb2.DecisionList()

    async def PollDirectives(self, request, context):
        directives, self.directives = self.directives, []
        return harkeniq_pb2.DirectiveList(directives=directives)

    async def ReportDirectiveResult(self, request, context):
        self.results.append(request)
        return harkeniq_pb2.DirectiveAck(accepted=True)


@pytest.fixture
async def dell_sim():
    sim = MockSimulator(device="dell-r750", port=0, no_auth=True)
    await sim.start()
    yield sim
    await sim.stop()


@pytest.fixture
async def sm():
    service = FakeDirectiveSM()
    server = grpc.aio.server()
    harkeniq_pb2_grpc.add_AgentServiceServicer_to_server(service, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    yield service, port
    await server.stop(grace=None)


def _make_agent(dell_sim, port, tmp_path, allow_list=None):
    skills_dir = tmp_path / "skills"
    shutil.copytree(REPO / "skills", skills_dir)
    config = {
        "agent": {"id": "agent-directive-test", "name": "rack-1-srv-1"},
        "bmc": {"host": dell_sim.url, "username": "admin",
                "password": "password", "verify_ssl": False},
        "skills": {"directory": str(skills_dir)},
        "polling": {"sensor_interval": 0.05},
        "checkpoint": {"path": str(tmp_path / "cp.db")},
        "site_manager": {"host": "127.0.0.1", "port": port, "tls": False,
                         "action_poll_interval": 0.05,
                         "heartbeat_interval": 0.05},
    }
    if allow_list is not None:
        config["actions"] = {"enabled": True, "approval_mode": "queue",
                             "allow_list": allow_list}
    return Agent(config)


def _directive(directive_id: str, **kwargs) -> harkeniq_pb2.Directive:
    return harkeniq_pb2.Directive(directive_id=directive_id, **kwargs)


async def _wait_until(predicate, timeout=15.0, message="condition not met"):
    # 15s: generous only on failure — the passing path returns as soon as
    # the predicate holds. 5s proved flaky under full-suite load once the
    # gate chain added a pre-execution sensor poll to directed actions.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(message)


async def _run_agent_until(agent, predicate, message):
    task = asyncio.create_task(agent.run(install_signal_handlers=False))
    try:
        await _wait_until(predicate, message=message)
    finally:
        agent.request_shutdown()
        await asyncio.wait_for(task, timeout=5.0)


class TestDirectedActions:
    async def test_directed_action_executes_and_settles(
        self, dell_sim, sm, tmp_path
    ):
        service, port = sm
        service.directives.append(_directive(
            "dir-led-1", kind="action", action_type="IDENTIFY_LED",
            params_json='{"target": "Disk.Bay.0:Enclosure.Internal.0-1:RAID.Slot.1-1"}',
            issued_by="test-campaign",
        ))
        agent = _make_agent(dell_sim, port, tmp_path)
        await agent.start()
        await _run_agent_until(
            agent, lambda: service.results, "directive never settled",
        )
        result = service.results[0]
        assert result.directive_id == "dir-led-1"
        assert result.success is True
        # The action really ran on the BMC
        assert dell_sim.action_state["led"]

        # Audit entry on the (verifiable) chain; agent stopped, reopen
        from harkeniq.state.checkpoint import CheckpointManager
        cp = CheckpointManager(tmp_path / "cp.db")
        entries = await cp.list_audit_entries()
        assert any(e["action"] == "DIRECTIVE_ACTION" for e in entries)
        assert (await cp.verify_audit_chain()).valid is True
        await cp.close()

    async def test_allow_list_still_applies(self, dell_sim, sm, tmp_path):
        service, port = sm
        service.directives.append(_directive(
            "dir-fw-1", kind="action", action_type="FIRMWARE_UPDATE",
            params_json='{"component": "bmc", "target_version": "7.10.30.00"}',
            issued_by="firmware-campaign:c1",
        ))
        # Default allow list: firmware actions NOT permitted
        agent = _make_agent(dell_sim, port, tmp_path)
        await agent.start()
        await _run_agent_until(
            agent, lambda: service.results, "directive never settled",
        )
        result = service.results[0]
        assert result.success is False
        assert "allow list" in result.detail
        assert dell_sim.firmware_banks["bmc"]["active"] == "7.00.00.00"

    async def test_directed_firmware_update_with_allow_list(
        self, dell_sim, sm, tmp_path
    ):
        service, port = sm
        service.directives.append(_directive(
            "dir-fw-2", kind="action", action_type="FIRMWARE_UPDATE",
            params_json='{"component": "bmc", "target_version": "7.10.30.00"}',
            issued_by="firmware-campaign:c1",
        ))
        agent = _make_agent(
            dell_sim, port, tmp_path,
            allow_list=["FIRMWARE_UPDATE", "FIRMWARE_ROLLBACK"],
        )
        await agent.start()
        agent.executor.task_poll_interval = 0.0
        await _run_agent_until(
            agent, lambda: service.results, "directive never settled",
        )
        assert service.results[0].success is True
        assert dell_sim.firmware_banks["bmc"]["active"] == "7.10.30.00"
        assert dell_sim.firmware_banks["bmc"]["standby"] == "7.00.00.00"

    async def test_unknown_action_type_fails_cleanly(
        self, dell_sim, sm, tmp_path
    ):
        service, port = sm
        service.directives.append(_directive(
            "dir-bad-1", kind="action", action_type="FORMAT_ALL_DISKS",
        ))
        agent = _make_agent(dell_sim, port, tmp_path)
        await agent.start()
        await _run_agent_until(
            agent, lambda: service.results, "directive never settled",
        )
        assert service.results[0].success is False
        assert "unknown action type" in service.results[0].detail


class TestDirectedSkillInstall:
    async def test_skill_installs_and_hot_loads(self, dell_sim, sm, tmp_path):
        service, port = sm
        service.directives.append(_directive(
            "dir-skill-1", kind="skill_install",
            skill_id="directed-fan-watch", skill_version="1",
            yaml_content=COMMUNITY_SKILL, tier="verified",
            validation_state="tested", issued_by="marketplace",
        ))
        agent = _make_agent(dell_sim, port, tmp_path)
        await agent.start()
        await _run_agent_until(
            agent, lambda: service.results, "directive never settled",
        )
        assert service.results[0].success is True
        # Written to the skills dir and hot-loaded into the engine
        assert (tmp_path / "skills" / "directed-fan-watch.yaml").exists()
        assert "directed-fan-watch" in agent.skill_engine._skills

    async def test_draft_skill_rejected(self, dell_sim, sm, tmp_path):
        service, port = sm
        service.directives.append(_directive(
            "dir-skill-2", kind="skill_install",
            skill_id="bad-skill", skill_version="1",
            yaml_content=COMMUNITY_SKILL, tier="community",
            validation_state="draft",
        ))
        agent = _make_agent(dell_sim, port, tmp_path)
        await agent.start()
        await _run_agent_until(
            agent, lambda: service.results, "directive never settled",
        )
        assert service.results[0].success is False
        assert "DRAFT" in service.results[0].detail
        assert not (tmp_path / "skills" / "bad-skill.yaml").exists()

    async def test_duplicate_delivery_executes_once(
        self, dell_sim, sm, tmp_path
    ):
        service, port = sm
        duplicate = _directive(
            "dir-dup-1", kind="action", action_type="IDENTIFY_LED",
            params_json='{"target": "Disk.Bay.0:Enclosure.Internal.0-1:RAID.Slot.1-1"}',
        )
        service.directives.append(duplicate)
        agent = _make_agent(dell_sim, port, tmp_path)
        await agent.start()

        async def _requeue_and_check():
            await _wait_until(lambda: service.results)
            # Re-deliver the same directive; the agent must dedupe
            service.directives.append(duplicate)
            await asyncio.sleep(0.3)

        task = asyncio.create_task(agent.run(install_signal_handlers=False))
        try:
            await _requeue_and_check()
        finally:
            agent.request_shutdown()
            await asyncio.wait_for(task, timeout=5.0)
        settled = [r for r in service.results
                   if r.directive_id == "dir-dup-1"]
        assert len(settled) == 1


class TestEndToEndCampaign:
    """The full production path: FirmwareOrchestrator -> DirectiveService
    -> SM gRPC -> agent poll -> ActionExecutor -> simulator UpdateService
    -> settle -> campaign completed. Nothing faked but the BMC."""

    async def test_campaign_flashes_real_agent(self, dell_sim, tmp_path):
        from harkeniq_sm.config import SMConfig
        from harkeniq_sm.db.base import (
            create_all, make_engine, make_sessionmaker,
        )
        from harkeniq_sm.db.repos import DeviceRepo, SiteRepo
        from harkeniq_sm.directives import (
            AgentDirectedUpdater, DirectiveService,
        )
        from harkeniq_sm.firmware_orchestrator import FirmwareOrchestrator
        from harkeniq_sm.grpc_server import (
            AgentServiceServicer, build_server,
        )
        from harkeniq_sm.ingest import IngestService

        engine = make_engine("sqlite+aiosqlite:///:memory:")
        await create_all(engine)
        db = make_sessionmaker(engine)
        sm_config = SMConfig(insecure=True, site_name="site-1",
                             grpc_host="127.0.0.1", grpc_port=0)
        ingest = IngestService(db, sm_config)
        directives = DirectiveService(db, sm_config)
        server, port = build_server(
            sm_config, AgentServiceServicer(ingest, directives=directives)
        )
        await server.start()

        agent = _make_agent(
            dell_sim, port, tmp_path,
            allow_list=["FIRMWARE_UPDATE", "FIRMWARE_ROLLBACK"],
        )
        await agent.start()
        agent.executor.task_poll_interval = 0.0
        agent_task = asyncio.create_task(
            agent.run(install_signal_handlers=False)
        )
        try:
            # Registration created the device row; find it
            async def _device_registered():
                async with db() as session:
                    device = await DeviceRepo(session).get_by_agent_id(
                        agent.agent_id  # R3a: key-derived id, not config id
                    )
                    return device.id if device else None

            await _wait_until(
                lambda: True, timeout=0.1,
            )  # let the loop spin up
            device_id = None
            deadline = time.monotonic() + 5.0
            while device_id is None and time.monotonic() < deadline:
                device_id = await _device_registered()
                await asyncio.sleep(0.05)
            assert device_id, "agent never registered with SM"

            async with db() as session:
                site = await SiteRepo(session).get_or_create("site-1")
                site_id = site.id

            updater = AgentDirectedUpdater(
                directives, timeout_s=10.0, poll_interval_s=0.05,
            )
            orch = FirmwareOrchestrator(db, updater=updater)
            campaign_id = await orch.create_campaign(
                site_id, [device_id], "bmc", "7.10.30.00",
            )
            await orch.approve(campaign_id, actor="vinod")
            result = await orch.advance(campaign_id)
            assert result["status"] == "completed", result

            # The real BMC flashed, blue-green bank preserved
            assert dell_sim.firmware_banks["bmc"]["active"] == "7.10.30.00"
            assert dell_sim.firmware_banks["bmc"]["standby"] == "7.00.00.00"
        finally:
            agent.request_shutdown()
            await asyncio.wait_for(agent_task, timeout=5.0)
            await server.stop(grace=None)
            await engine.dispose()
