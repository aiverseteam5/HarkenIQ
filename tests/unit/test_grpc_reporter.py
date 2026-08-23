"""Site Manager gRPC reporter tests (Doc 06 §10.2)."""

import json

import grpc
import pytest

from harkeniq.models import Evidence, Verdict, VerdictSeverity
from harkeniq.proto import harkeniq_pb2, harkeniq_pb2_grpc
from harkeniq.reporting.grpc_stub import BACKOFF_SCHEDULE, SiteManagerReporter


class RecordingService(harkeniq_pb2_grpc.AgentServiceServicer):
    """In-process AgentService that records every request."""

    def __init__(self, accept=True):
        self.accept = accept
        self.verdicts = []
        self.heartbeats = []

    async def ReportVerdict(self, request, context):
        self.verdicts.append(request)
        return harkeniq_pb2.VerdictAck(accepted=self.accept)

    async def Heartbeat(self, request, context):
        self.heartbeats.append(request)
        return harkeniq_pb2.HeartbeatAck(accepted=self.accept)


@pytest.fixture
async def sm_server():
    """Start an in-process Site Manager gRPC server on an ephemeral port."""
    service = RecordingService()
    server = grpc.aio.server()
    harkeniq_pb2_grpc.add_AgentServiceServicer_to_server(service, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    yield service, port
    await server.stop(grace=None)


def make_reporter(port, host="127.0.0.1", timeout=5.0):
    return SiteManagerReporter(
        {
            "site_manager": {"host": host, "port": port},
            "agent": {"id": "agent-aaaa", "name": "rack-12-server-04"},
        },
        request_timeout=timeout,
    )


def make_verdict():
    return Verdict(
        sensor_id="disk:sda",
        skill_name="disk-health",
        severity=VerdictSeverity.CRITICAL,
        message="Drive failure predicted",
        evidence=[Evidence(
            sensor_id="disk:sda", skill_name="disk-health", rule_index=0,
            condition="health == 'Critical'", fields={"health": "Critical"},
            timestamp="2026-08-19T10:00:00Z",
        )],
        timestamp="2026-08-19T10:00:00Z",
    )


class TestDelivery:
    async def test_verdict_delivered(self, sm_server):
        service, port = sm_server
        reporter = make_reporter(port)
        assert await reporter.report_verdict(make_verdict()) is True
        await reporter.close()

        assert len(service.verdicts) == 1
        req = service.verdicts[0]
        assert req.agent_id == "agent-aaaa"
        assert req.sensor_id == "disk:sda"
        assert req.skill_name == "disk-health"
        assert req.verdict == "CRITICAL"
        assert req.timestamp_unix > 0
        payload = json.loads(req.evidence_json)
        assert payload["message"] == "Drive failure predicted"
        assert payload["evidence"][0]["fields"] == {"health": "Critical"}

    async def test_heartbeat_delivered(self, sm_server):
        service, port = sm_server
        reporter = make_reporter(port)
        assert await reporter.send_heartbeat("OBSERVING", {"fan": "OK", "disk": "WARNING"}) is not None
        await reporter.close()

        assert len(service.heartbeats) == 1
        req = service.heartbeats[0]
        assert req.agent_id == "agent-aaaa"
        assert req.agent_name == "rack-12-server-04"
        assert req.state == "OBSERVING"
        assert dict(req.health_summary) == {"fan": "OK", "disk": "WARNING"}
        assert req.timestamp_unix > 0

    async def test_verdict_timestamp_parsed(self, sm_server):
        service, port = sm_server
        reporter = make_reporter(port)
        await reporter.report_verdict(make_verdict())
        await reporter.close()
        # 2026-08-19T10:00:00Z
        assert service.verdicts[0].timestamp_unix == 1787133600

    async def test_not_accepted_returns_false(self, sm_server):
        service, port = sm_server
        service.accept = False
        reporter = make_reporter(port)
        assert await reporter.report_verdict(make_verdict()) is False
        await reporter.close()


class TestStandalone:
    async def test_disabled_without_host(self):
        reporter = SiteManagerReporter({"site_manager": {"host": "", "port": 50051}})
        assert reporter.enabled is False
        assert await reporter.report_verdict(make_verdict()) is False
        assert await reporter.send_heartbeat("OBSERVING", {}) is None
        assert reporter._channel is None  # never connected
        await reporter.close()

    async def test_disabled_with_empty_config(self):
        reporter = SiteManagerReporter({})
        assert reporter.enabled is False


class TestBackoff:
    async def test_backoff_schedule_on_failure(self, unused_tcp_port):
        reporter = make_reporter(unused_tcp_port, timeout=1.0)
        fake_now = [1000.0]
        reporter._clock = lambda: fake_now[0]

        for expected_delay in BACKOFF_SCHEDULE:
            assert await reporter.report_verdict(make_verdict()) is False
            assert reporter._next_attempt == fake_now[0] + expected_delay
            fake_now[0] = reporter._next_attempt
        # schedule capped at 300s
        assert await reporter.report_verdict(make_verdict()) is False
        assert reporter._next_attempt == fake_now[0] + BACKOFF_SCHEDULE[-1]
        await reporter.close()

    async def test_drops_without_rpc_during_backoff(self, unused_tcp_port):
        reporter = make_reporter(unused_tcp_port, timeout=1.0)
        fake_now = [1000.0]
        reporter._clock = lambda: fake_now[0]

        await reporter.send_heartbeat("OBSERVING", {})
        dropped = reporter.dropped
        # still inside the backoff window: dropped immediately, no RPC attempt
        fake_now[0] += 1.0
        assert await reporter.send_heartbeat("OBSERVING", {}) is None
        assert reporter.dropped == dropped + 1
        assert reporter._backoff_index == 1  # unchanged, no new failure recorded
        await reporter.close()

    async def test_success_resets_backoff(self, sm_server):
        service, port = sm_server
        reporter = make_reporter(port)
        reporter._backoff_index = 3
        reporter._next_attempt = 0.0
        assert await reporter.send_heartbeat("OBSERVING", {}) is not None
        assert reporter._backoff_index == 0
        assert reporter._next_attempt == 0.0
        await reporter.close()
