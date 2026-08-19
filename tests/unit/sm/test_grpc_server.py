"""SM gRPC receiver: token interceptor, TLS, ingest round-trips."""

import grpc
import pytest

from harkeniq.proto import harkeniq_pb2, harkeniq_pb2_grpc
from harkeniq_sm.config import SMConfig
from harkeniq_sm.grpc_server import AgentServiceServicer, build_server
from harkeniq_sm.ingest import IngestService
from harkeniq_sm.db.repos import DeviceRepo, StatusRepo, TelemetryRepo

TOKEN = "site-secret"


def _config(**overrides):
    defaults = dict(grpc_host="127.0.0.1", grpc_port=0, insecure=True)
    defaults.update(overrides)
    return SMConfig(**defaults)


@pytest.fixture
async def served(db):
    """(config-factory driven) started server + port + db, insecure + token."""
    config = _config(site_token=TOKEN)
    ingest = IngestService(db, config)
    server, port = build_server(config, AgentServiceServicer(ingest))
    await server.start()
    yield port
    await server.stop(grace=None)


def _bearer(token=TOKEN):
    return (("authorization", f"Bearer {token}"),)


class TestAuth:
    async def test_valid_token_accepted(self, served):
        async with grpc.aio.insecure_channel(f"127.0.0.1:{served}") as channel:
            stub = harkeniq_pb2_grpc.AgentServiceStub(channel)
            ack = await stub.RegisterAgent(
                harkeniq_pb2.AgentRegistration(agent_id="a1"), metadata=_bearer()
            )
        assert ack.accepted is True

    @pytest.mark.parametrize("metadata", [(), _bearer("wrong")])
    async def test_bad_token_unauthenticated(self, served, metadata):
        async with grpc.aio.insecure_channel(f"127.0.0.1:{served}") as channel:
            stub = harkeniq_pb2_grpc.AgentServiceStub(channel)
            with pytest.raises(grpc.aio.AioRpcError) as exc:
                await stub.RegisterAgent(
                    harkeniq_pb2.AgentRegistration(agent_id="a1"), metadata=metadata
                )
        assert exc.value.code() == grpc.StatusCode.UNAUTHENTICATED

    def test_secure_config_requires_token(self):
        config = _config(insecure=False)
        with pytest.raises(ValueError):
            build_server(config, AgentServiceServicer(None))


class TestTls:
    async def test_tls_round_trip(self, db, dev_tls):
        config = _config(
            site_token=TOKEN, insecure=False,
            tls_cert=dev_tls["cert"], tls_key=dev_tls["key"],
        )
        server, port = build_server(
            config, AgentServiceServicer(IngestService(db, config))
        )
        await server.start()
        try:
            creds = grpc.ssl_channel_credentials(
                root_certificates=open(dev_tls["ca"], "rb").read()
            )
            async with grpc.aio.secure_channel(
                f"localhost:{port}", creds
            ) as channel:
                stub = harkeniq_pb2_grpc.AgentServiceStub(channel)
                ack = await stub.RegisterAgent(
                    harkeniq_pb2.AgentRegistration(agent_id="a1"),
                    metadata=_bearer(),
                )
            assert ack.accepted is True
        finally:
            await server.stop(grace=None)


class TestIngestRpcs:
    async def test_heartbeat_and_verdict_persist(self, db, served):
        async with grpc.aio.insecure_channel(f"127.0.0.1:{served}") as channel:
            stub = harkeniq_pb2_grpc.AgentServiceStub(channel)
            hb = await stub.Heartbeat(
                harkeniq_pb2.AgentHeartbeat(
                    agent_id="a1", agent_name="srv-1", state="OBSERVING",
                    health_summary={"psu": "OK"}, peer_status={"p1": "ALIVE"},
                ),
                metadata=_bearer(),
            )
            vd = await stub.ReportVerdict(
                harkeniq_pb2.VerdictReport(
                    agent_id="a1", sensor_id="psu:PS1", skill_name="psu_health",
                    verdict="CRITICAL", evidence_json="[]",
                ),
                metadata=_bearer(),
            )
        assert hb.accepted and vd.accepted
        async with db() as session:
            device = await DeviceRepo(session).get_by_agent_id("a1")
            assert (await StatusRepo(session).get(device.id)).last_state == "OBSERVING"
            verdicts = await TelemetryRepo(session).recent_verdicts(device.id)
            assert verdicts[0].severity == "CRITICAL"

    async def test_action_rpcs_before_approvals_attached(self, served):
        async with grpc.aio.insecure_channel(f"127.0.0.1:{served}") as channel:
            stub = harkeniq_pb2_grpc.AgentServiceStub(channel)
            ack = await stub.ReportAction(
                harkeniq_pb2.ActionReport(agent_id="a1", action_id="x"),
                metadata=_bearer(),
            )
            decisions = await stub.PollActionDecisions(
                harkeniq_pb2.DecisionPoll(agent_id="a1"), metadata=_bearer()
            )
        assert ack.accepted is False
        assert list(decisions.decisions) == []
