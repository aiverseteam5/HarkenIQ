"""SM directed-directive transport tests (R5).

Covers: DirectiveService queue/deliver/settle semantics, the gRPC
servicer surface over a real server, the AgentDirectedUpdater driving a
firmware campaign over directives, and the skill-install API.
"""

from __future__ import annotations

import asyncio

import grpc
import pytest
from httpx import ASGITransport, AsyncClient

from harkeniq.proto import harkeniq_pb2, harkeniq_pb2_grpc
from harkeniq_sm.app import create_app
from harkeniq_sm.config import SMConfig
from harkeniq_sm.db.repos import AuditRepo, DeviceRepo, SiteRepo
from harkeniq_sm.directives import AgentDirectedUpdater, DirectiveService
from harkeniq_sm.firmware_orchestrator import FirmwareOrchestrator
from harkeniq_sm.grpc_server import AgentServiceServicer, build_server
from harkeniq_sm.ingest import IngestService
from harkeniq_sm.runtime import AppState

TOKEN = "site-secret"

SKILL_YAML = """\
name: pushed-skill
version: 1
target: fan
rules:
  - condition: "health == 'Warning'"
    verdict: WARNING
    message: "Fan {name} degraded"
default_verdict: HEALTHY
"""


def _config(**overrides):
    defaults = dict(insecure=True, site_name="site-1",
                    directive_poll_interval_s=0.02)
    defaults.update(overrides)
    return SMConfig(**defaults)


async def _seed_device(db, agent_id="agent-1"):
    async with db() as session:
        site = await SiteRepo(session).get_or_create("site-1")
        device = await DeviceRepo(session).upsert_registration(
            site_id=site.id, agent_id=agent_id, vendor="dell",
            firmware=[{"component": "bmc", "name": "iDRAC9",
                       "version": "7.00.00.00"}],
        )
        await session.commit()
        return device.id


class TestDirectiveService:
    async def test_enqueue_poll_settle(self, db):
        device_id = await _seed_device(db)
        svc = DirectiveService(db, _config())
        directive_id = await svc.enqueue_action(
            device_id, "IDENTIFY_LED", {"target": "d0"},
            issued_by="test",
        )
        delivered = await svc.poll("agent-1")
        assert len(delivered) == 1
        assert delivered[0].directive_id == directive_id
        assert delivered[0].action_type == "IDENTIFY_LED"
        # Delivered exactly once
        assert await svc.poll("agent-1") == []

        assert await svc.report_result("agent-1", directive_id, True, "ok")
        status, detail = await svc.get_status(directive_id)
        assert status == "completed"
        assert detail == "ok"

    async def test_unknown_agent_gets_nothing(self, db):
        await _seed_device(db)
        svc = DirectiveService(db, _config())
        assert await svc.poll("ghost-agent") == []

    async def test_wrong_agent_cannot_settle(self, db):
        device_id = await _seed_device(db)
        await _seed_device(db, agent_id="agent-2")
        svc = DirectiveService(db, _config())
        directive_id = await svc.enqueue_action(device_id, "SEL_CLEAR")
        await svc.poll("agent-1")
        assert not await svc.report_result("agent-2", directive_id, True, "")
        status, _ = await svc.get_status(directive_id)
        assert status == "delivered"

    async def test_double_settle_rejected(self, db):
        device_id = await _seed_device(db)
        svc = DirectiveService(db, _config())
        directive_id = await svc.enqueue_action(device_id, "SEL_CLEAR")
        await svc.poll("agent-1")
        assert await svc.report_result("agent-1", directive_id, False, "boom")
        assert not await svc.report_result("agent-1", directive_id, True, "")
        status, _ = await svc.get_status(directive_id)
        assert status == "failed"

    async def test_wait_for_completion_timeout(self, db):
        device_id = await _seed_device(db)
        svc = DirectiveService(db, _config())
        directive_id = await svc.enqueue_action(device_id, "SEL_CLEAR")
        status, detail = await svc.wait_for_completion(
            directive_id, timeout_s=0.1, poll_interval_s=0.02,
        )
        assert status == "timed_out"

    async def test_lifecycle_is_audited_on_chain(self, db):
        device_id = await _seed_device(db)
        svc = DirectiveService(db, _config())
        directive_id = await svc.enqueue_action(device_id, "SEL_CLEAR",
                                                issued_by="campaign:x")
        await svc.poll("agent-1")
        await svc.report_result("agent-1", directive_id, True, "ok")
        async with db() as session:
            audit = AuditRepo(session)
            actions = [r.action for r in await audit.list_all()]
            assert "directive.enqueue" in actions
            assert "directive.result" in actions
            assert (await audit.verify_chain()).valid is True


class TestDirectiveRPCs:
    @pytest.fixture
    async def served(self, db):
        config = _config(site_token=TOKEN)
        ingest = IngestService(db, config)
        directives = DirectiveService(db, config)
        server, port = build_server(
            config, AgentServiceServicer(ingest, directives=directives)
        )
        await server.start()
        yield port, directives, db
        await server.stop(grace=None)

    def _bearer(self):
        return (("authorization", f"Bearer {TOKEN}"),)

    async def test_poll_and_settle_over_grpc(self, served):
        port, directives, db = served
        device_id = await _seed_device(db)
        directive_id = await directives.enqueue_action(
            device_id, "IDENTIFY_LED", {"target": "d0"},
        )
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = harkeniq_pb2_grpc.AgentServiceStub(channel)
            response = await stub.PollDirectives(
                harkeniq_pb2.DirectivePoll(agent_id="agent-1"),
                metadata=self._bearer(),
            )
            assert len(response.directives) == 1
            ack = await stub.ReportDirectiveResult(
                harkeniq_pb2.DirectiveResult(
                    agent_id="agent-1", directive_id=directive_id,
                    success=True, detail="done",
                ),
                metadata=self._bearer(),
            )
            assert ack.accepted is True
        status, _ = await directives.get_status(directive_id)
        assert status == "completed"


class TestAgentDirectedUpdater:
    async def test_campaign_completes_over_directive_transport(self, tmp_path):
        """The R4-3 seam closed: campaign advance drives directives,
        a simulated agent settles them, the campaign completes.

        File-backed sqlite on purpose: the updater's wait loop and the
        fake agent's settle loop run concurrent sessions, and in-memory
        sqlite rides ONE shared connection (the documented StaticPool
        hazard) -- interleaved transactions make the test flaky.
        """
        from harkeniq_sm.db.base import create_all as _create_all
        from harkeniq_sm.db.base import make_engine as _make_engine
        from harkeniq_sm.db.base import make_sessionmaker as _make_sessionmaker

        engine = _make_engine(f"sqlite+aiosqlite:///{tmp_path}/sm.db")
        await _create_all(engine)
        db = _make_sessionmaker(engine)
        device_id = await _seed_device(db)
        svc = DirectiveService(db, _config())
        updater = AgentDirectedUpdater(svc, timeout_s=5.0,
                                       poll_interval_s=0.02)
        orch = FirmwareOrchestrator(db, updater=updater)
        async with db() as session:
            site = await SiteRepo(session).get_or_create("site-1")
            site_id = site.id
        campaign_id = await orch.create_campaign(
            site_id, [device_id], "bmc", "7.10.30.00",
        )
        await orch.approve(campaign_id, actor="vinod")

        async def _fake_agent():
            """Poll for directives and settle them like an agent would."""
            for _ in range(200):
                delivered = await svc.poll("agent-1")
                for directive in delivered:
                    await svc.report_result(
                        "agent-1", directive.directive_id, True, "flashed",
                    )
                await asyncio.sleep(0.02)

        agent_task = asyncio.create_task(_fake_agent())
        try:
            result = await orch.advance(campaign_id)
        finally:
            agent_task.cancel()
            await engine.dispose()
        assert result["status"] == "completed"

    async def test_unpolled_directive_times_out_and_halts(self, db):
        device_id = await _seed_device(db)
        svc = DirectiveService(db, _config())
        updater = AgentDirectedUpdater(svc, timeout_s=0.1,
                                       poll_interval_s=0.02)
        orch = FirmwareOrchestrator(db, updater=updater)
        async with db() as session:
            site = await SiteRepo(session).get_or_create("site-1")
            site_id = site.id
        campaign_id = await orch.create_campaign(
            site_id, [device_id], "bmc", "7.10.30.00",
        )
        await orch.approve(campaign_id, actor="vinod")
        result = await orch.advance(campaign_id)
        assert result["status"] == "halted"
        assert "timed_out" in result["halt_reason"]


@pytest.fixture
async def client(db):
    config = _config()
    state = AppState(config=config, sessionmaker=db)
    state.directives = DirectiveService(db, config)
    app = create_app(state)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.db = db
        c.directives = state.directives
        yield c


class TestSkillInstallAPI:
    async def test_install_queues_directives_for_all_agents(self, client):
        await _seed_device(client.db, "agent-1")
        await _seed_device(client.db, "agent-2")
        r = await client.post("/api/skills/install", json={
            "skill_id": "pushed-skill", "skill_version": "1",
            "yaml_content": SKILL_YAML, "tier": "verified",
            "validation_state": "tested", "agent_ids": "all",
        })
        assert r.status_code == 200, r.text
        assert r.json()["count"] == 2
        delivered = await client.directives.poll("agent-1")
        assert len(delivered) == 1
        assert delivered[0].kind == "skill_install"
        assert delivered[0].yaml_content == SKILL_YAML

    async def test_invalid_yaml_rejected_before_queueing(self, client):
        await _seed_device(client.db, "agent-1")
        r = await client.post("/api/skills/install", json={
            "skill_id": "bad", "yaml_content": "not: [valid",
        })
        assert r.status_code == 422
        assert await client.directives.poll("agent-1") == []

    async def test_unknown_agent_404(self, client):
        r = await client.post("/api/skills/install", json={
            "skill_id": "x", "yaml_content": SKILL_YAML,
            "agent_ids": ["ghost"],
        })
        assert r.status_code == 404
