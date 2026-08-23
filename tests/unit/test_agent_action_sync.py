"""Agent action sync with the Site Manager (R2a R-S6, D16)."""

from pathlib import Path

import grpc
import pytest

from harkeniq.agent import Agent
from harkeniq.mock.simulator import MockSimulator
from harkeniq.models import ActionStatus, ActionType, VerdictSeverity
from harkeniq.proto import harkeniq_pb2, harkeniq_pb2_grpc

REPO = Path(__file__).parents[2]


class FakeSM(harkeniq_pb2_grpc.AgentServiceServicer):
    """Records action reports; serves queued decisions once."""

    def __init__(self):
        self.action_reports = []
        self.decisions = []

    async def RegisterAgent(self, request, context):
        return harkeniq_pb2.RegistrationAck(accepted=True, site_name="site-1")

    async def Heartbeat(self, request, context):
        return harkeniq_pb2.HeartbeatAck(accepted=True)

    async def ReportAction(self, request, context):
        self.action_reports.append(request)
        return harkeniq_pb2.ActionAck(accepted=True)

    async def PollActionDecisions(self, request, context):
        decisions, self.decisions = self.decisions, []
        return harkeniq_pb2.DecisionList(decisions=decisions)


@pytest.fixture
async def dell_sim():
    sim = MockSimulator(device="dell-r750", port=0, no_auth=True)
    await sim.start()
    yield sim
    await sim.stop()


@pytest.fixture
async def sm():
    service = FakeSM()
    server = grpc.aio.server()
    harkeniq_pb2_grpc.add_AgentServiceServicer_to_server(service, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    yield service, port
    await server.stop(grace=None)


@pytest.fixture
async def agent(dell_sim, sm, tmp_path):
    _, port = sm
    config = {
        "agent": {"id": "agent-sync-test", "name": "rack-1-srv-1"},
        "bmc": {"host": dell_sim.url, "username": "admin", "password": "password",
                "verify_ssl": False},
        "skills": {"directory": str(REPO / "skills")},
        "polling": {"sensor_interval": 0.05},
        "checkpoint": {"path": str(tmp_path / "cp.db")},
        "site_manager": {"host": "127.0.0.1", "port": port, "tls": False,
                         "action_poll_interval": 0.05},
    }
    a = Agent(config)
    await a.start()
    yield a
    await a.stop()


def _enqueue(agent):
    return agent.action_queue.enqueue(
        ActionType.FAN_RESET, "fan:Fan1", "fan_health", VerdictSeverity.CRITICAL,
        {"target": "Fan1"},
    )


def _decision(action_id, decision="approved", by="op@site"):
    return harkeniq_pb2.ActionDecision(
        action_id=action_id, decision=decision, decided_by=by,
        decided_at_unix=1755600000,
    )


class TestReporting:
    async def test_reports_pending_once(self, agent, sm):
        service, _ = sm
        action = _enqueue(agent)
        await agent._sync_actions()
        await agent._sync_actions()  # unchanged → not re-reported
        reports = [r for r in service.action_reports if r.action_id == action.id]
        assert len(reports) == 1
        report = reports[0]
        assert report.status == "PENDING"
        assert report.type == "FAN_RESET"
        assert report.agent_id == agent.agent_id

    async def test_status_change_reported(self, agent, sm):
        service, _ = sm
        action = _enqueue(agent)
        await agent._sync_actions()
        agent.action_queue.approve(action.id)
        await agent._sync_actions()
        statuses = [
            r.status for r in service.action_reports if r.action_id == action.id
        ]
        assert statuses == ["PENDING", "APPROVED"]


class TestDecisionApplication:
    async def test_approve_applied(self, agent, sm):
        service, _ = sm
        action = _enqueue(agent)
        await agent._sync_actions()
        service.decisions = [_decision(action.id)]
        await agent._sync_actions()
        assert agent.action_queue.get(action.id).status == ActionStatus.APPROVED
        # Audit carries the SM authorization.
        rows = agent.checkpoint._conn.execute("SELECT * FROM audit_log").fetchall()
        assert any(r["authorization"] == "sm:op@site" for r in rows)

    async def test_deny_applied(self, agent, sm):
        service, _ = sm
        action = _enqueue(agent)
        await agent._sync_actions()
        service.decisions = [_decision(action.id, decision="denied")]
        await agent._sync_actions()
        assert agent.action_queue.get(action.id).status == ActionStatus.DENIED

    async def test_cli_race_skipped(self, agent, sm):
        """Action already decided locally → SM decision is not applied."""
        service, _ = sm
        action = _enqueue(agent)
        agent.action_queue.deny(action.id)  # CLI got there first
        await agent.checkpoint.save_actions(agent.action_queue.all())
        service.decisions = [_decision(action.id, decision="approved")]
        await agent._sync_actions()
        assert agent.action_queue.get(action.id).status == ActionStatus.DENIED

    async def test_unknown_action_ignored(self, agent, sm):
        service, _ = sm
        service.decisions = [_decision("act-ghost")]
        await agent._sync_actions()  # must not raise


class TestPollCadence:
    async def test_in_flight_detection(self, agent):
        assert agent._actions_in_flight() is False
        action = _enqueue(agent)
        assert agent._actions_in_flight() is True
        agent.action_queue.deny(action.id)
        assert agent._actions_in_flight() is False
