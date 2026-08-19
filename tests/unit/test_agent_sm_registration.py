"""Agent -> Site Manager registration at startup (R2a, spec 00 §7 / A1)."""

from pathlib import Path

import grpc
import pytest

from harkeniq.agent import Agent
from harkeniq.mock.simulator import MockSimulator
from harkeniq.proto import harkeniq_pb2, harkeniq_pb2_grpc

REPO = Path(__file__).parents[2]


class RecordingService(harkeniq_pb2_grpc.AgentServiceServicer):
    def __init__(self):
        self.registrations = []
        self.heartbeats = []

    async def RegisterAgent(self, request, context):
        self.registrations.append(request)
        return harkeniq_pb2.RegistrationAck(accepted=True, site_name="site-1")

    async def Heartbeat(self, request, context):
        self.heartbeats.append(request)
        return harkeniq_pb2.HeartbeatAck(accepted=True)


@pytest.fixture
async def dell_sim():
    sim = MockSimulator(device="dell-r750", port=0, no_auth=True)
    await sim.start()
    yield sim
    await sim.stop()


@pytest.fixture
async def sm_server():
    service = RecordingService()
    server = grpc.aio.server()
    harkeniq_pb2_grpc.add_AgentServiceServicer_to_server(service, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    yield service, port
    await server.stop(grace=None)


def make_config(sim, sm_port=None):
    config = {
        "agent": {"id": "agent-reg-test", "name": "rack-1-srv-1"},
        "bmc": {"host": sim.url, "username": "admin", "password": "password",
                "verify_ssl": False},
        "skills": {"directory": str(REPO / "skills")},
        "polling": {"sensor_interval": 0.05},
    }
    if sm_port is not None:
        config["site_manager"] = {"host": "127.0.0.1", "port": sm_port, "tls": False}
    return config


class TestRegistration:
    async def test_registers_on_start(self, dell_sim, sm_server):
        service, port = sm_server
        agent = Agent(make_config(dell_sim, sm_port=port))
        await agent.start()
        try:
            assert len(service.registrations) == 1
            req = service.registrations[0]
            assert req.agent_id == "agent-reg-test"
            assert req.agent_name == "rack-1-srv-1"
            assert req.vendor == "dell"
            assert req.model  # detected from the simulator
        finally:
            await agent.stop()

    async def test_registration_failure_not_fatal(self, dell_sim, unused_tcp_port, caplog):
        # SM unreachable: agent must still come up standalone
        agent = Agent(make_config(dell_sim, sm_port=unused_tcp_port))
        agent_started = False
        import logging
        with caplog.at_level(logging.WARNING):
            await agent.start()
            agent_started = True
        try:
            assert agent_started
            assert any("registration failed" in r.message for r in caplog.records)
        finally:
            await agent.stop()

    async def test_no_registration_without_site_manager(self, dell_sim):
        agent = Agent(make_config(dell_sim))
        await agent.start()
        try:
            assert agent.reporter.enabled is False
        finally:
            await agent.stop()
