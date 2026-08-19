"""R2a Site Manager reporter features: TLS, bearer token, new RPCs (spec 00 §7)."""

import json

import grpc
import pytest

from harkeniq.models import (
    Action,
    ActionOutcome,
    ActionStatus,
    ActionType,
    VerdictSeverity,
)
from harkeniq.proto import harkeniq_pb2, harkeniq_pb2_grpc
from harkeniq.reporting.grpc_stub import SiteManagerReporter


class RecordingService(harkeniq_pb2_grpc.AgentServiceServicer):
    """In-process AgentService recording R2a requests and metadata."""

    def __init__(self):
        self.heartbeats = []
        self.registrations = []
        self.action_reports = []
        self.decision_polls = []
        self.metadata = []
        self.decisions_to_return = []

    def _record_meta(self, context):
        self.metadata.append(dict(context.invocation_metadata()))

    async def Heartbeat(self, request, context):
        self._record_meta(context)
        self.heartbeats.append(request)
        return harkeniq_pb2.HeartbeatAck(accepted=True)

    async def RegisterAgent(self, request, context):
        self._record_meta(context)
        self.registrations.append(request)
        return harkeniq_pb2.RegistrationAck(accepted=True, site_name="site-1")

    async def ReportAction(self, request, context):
        self._record_meta(context)
        self.action_reports.append(request)
        return harkeniq_pb2.ActionAck(accepted=True)

    async def PollActionDecisions(self, request, context):
        self._record_meta(context)
        self.decision_polls.append(request)
        return harkeniq_pb2.DecisionList(decisions=self.decisions_to_return)


@pytest.fixture
async def sm_server():
    service = RecordingService()
    server = grpc.aio.server()
    harkeniq_pb2_grpc.add_AgentServiceServicer_to_server(service, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    yield service, port
    await server.stop(grace=None)


def make_reporter(port, token="", host="127.0.0.1", **sm_extra):
    sm = {"site_manager": {"host": host, "port": port, "token": token, **sm_extra}}
    return SiteManagerReporter(
        {**sm, "agent": {"id": "agent-aaaa", "name": "rack-12-server-04"}},
        request_timeout=5.0,
    )


def make_action():
    return Action(
        id="act-0001",
        type=ActionType.FAN_RESET,
        params={"fan_id": "Fan.1A"},
        status=ActionStatus.PENDING,
        sensor_id="fan:Fan.1A",
        skill_name="fan-health",
        verdict_severity=VerdictSeverity.CRITICAL,
        proposed_at="2026-08-19T10:00:00Z",
    )


class TestRegistration:
    async def test_register_agent_delivered(self, sm_server):
        service, port = sm_server
        reporter = make_reporter(port)
        ok = await reporter.register_agent(
            vendor="dell",
            model="PowerEdge R750",
            service_tag="ABC1234",
            bmc_location={"rack": "12"},
            peers=["10.0.0.2:5150"],
        )
        await reporter.close()
        assert ok is True
        req = service.registrations[0]
        assert req.agent_id == "agent-aaaa"
        assert req.agent_name == "rack-12-server-04"
        assert req.vendor == "dell"
        assert req.model == "PowerEdge R750"
        assert req.service_tag == "ABC1234"
        assert json.loads(req.bmc_location_json) == {"rack": "12"}
        assert list(req.peers) == ["10.0.0.2:5150"]

    async def test_register_disabled_without_host(self):
        reporter = SiteManagerReporter({})
        assert await reporter.register_agent() is False


class TestHeartbeatPeerStatus:
    async def test_peer_status_delivered(self, sm_server):
        service, port = sm_server
        reporter = make_reporter(port)
        assert await reporter.send_heartbeat(
            "OBSERVING", {"fan": "OK"}, {"peer-b": "ALIVE", "peer-c": "UNRESPONSIVE"}
        )
        await reporter.close()
        req = service.heartbeats[0]
        assert dict(req.peer_status) == {"peer-b": "ALIVE", "peer-c": "UNRESPONSIVE"}

    async def test_peer_status_optional(self, sm_server):
        service, port = sm_server
        reporter = make_reporter(port)
        assert await reporter.send_heartbeat("OBSERVING", {"fan": "OK"})
        await reporter.close()
        assert dict(service.heartbeats[0].peer_status) == {}


class TestActionSync:
    async def test_report_action_delivered(self, sm_server):
        service, port = sm_server
        reporter = make_reporter(port)
        assert await reporter.report_action(make_action()) is True
        await reporter.close()
        req = service.action_reports[0]
        assert req.action_id == "act-0001"
        assert req.type == "FAN_RESET"
        assert req.sensor_id == "fan:Fan.1A"
        assert req.skill_name == "fan-health"
        assert req.verdict_severity == "CRITICAL"
        assert json.loads(req.params_json) == {"fan_id": "Fan.1A"}
        assert req.status == "PENDING"
        assert req.proposed_at == "2026-08-19T10:00:00Z"
        assert req.outcome_json == ""

    async def test_report_completed_action_serializes_outcome(self, sm_server):
        """Regression: ActionOutcome carries enums (type=ActionType)."""
        service, port = sm_server
        reporter = make_reporter(port)
        action = make_action()
        action.status = ActionStatus.COMPLETED
        action.outcome = ActionOutcome(
            action_id="act-0001",
            type=ActionType.FAN_RESET,
            target="Fan.1A",
            success=True,
            duration_ms=12.5,
        )
        assert await reporter.report_action(action) is True
        await reporter.close()
        req = service.action_reports[0]
        assert req.status == "COMPLETED"
        outcome = json.loads(req.outcome_json)
        assert outcome["type"] == "FAN_RESET"
        assert outcome["success"] is True

    async def test_poll_decisions_returns_list(self, sm_server):
        service, port = sm_server
        service.decisions_to_return = [
            harkeniq_pb2.ActionDecision(
                action_id="act-0001",
                decision="approved",
                decided_by="operator-1",
                decided_at_unix=1787133600,
            )
        ]
        reporter = make_reporter(port)
        decisions = await reporter.poll_decisions()
        await reporter.close()
        assert len(decisions) == 1
        assert decisions[0].action_id == "act-0001"
        assert decisions[0].decision == "approved"
        assert decisions[0].decided_by == "operator-1"

    async def test_poll_decisions_empty_on_failure(self, unused_tcp_port):
        reporter = make_reporter(unused_tcp_port)
        reporter.request_timeout = 1.0
        assert await reporter.poll_decisions() == []
        await reporter.close()

    async def test_poll_decisions_disabled(self):
        reporter = SiteManagerReporter({})
        assert await reporter.poll_decisions() == []


class TestAuthMetadata:
    async def test_bearer_token_attached(self, sm_server):
        service, port = sm_server
        reporter = make_reporter(port, token="site-secret")
        await reporter.send_heartbeat("OBSERVING", {})
        await reporter.close()
        assert service.metadata[0].get("authorization") == "Bearer site-secret"

    async def test_no_token_no_header(self, sm_server):
        service, port = sm_server
        reporter = make_reporter(port)
        await reporter.send_heartbeat("OBSERVING", {})
        await reporter.close()
        assert "authorization" not in service.metadata[0]


class TestTLS:
    async def test_secure_channel_round_trip(self, dev_tls):
        service = RecordingService()
        server = grpc.aio.server()
        harkeniq_pb2_grpc.add_AgentServiceServicer_to_server(service, server)
        with open(dev_tls["key"], "rb") as f:
            key = f.read()
        with open(dev_tls["cert"], "rb") as f:
            cert = f.read()
        creds = grpc.ssl_server_credentials([(key, cert)])
        port = server.add_secure_port("127.0.0.1:0", creds)
        await server.start()
        try:
            reporter = make_reporter(
                port, token="site-secret", host="localhost",
                tls=True, tls_ca=dev_tls["ca"],
            )
            assert reporter.tls is True
            assert await reporter.send_heartbeat("OBSERVING", {"fan": "OK"}) is True
            await reporter.close()
            assert service.metadata[0].get("authorization") == "Bearer site-secret"
        finally:
            await server.stop(grace=None)

    async def test_missing_ca_falls_back_insecure(self, sm_server):
        # tls requested but no CA provided -> insecure channel (bare R1 configs)
        service, port = sm_server
        reporter = make_reporter(port, tls=True)
        assert reporter.tls is False
        assert await reporter.send_heartbeat("OBSERVING", {}) is True
        await reporter.close()
