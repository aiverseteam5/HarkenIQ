"""Marketplace install sync tests (R5-2, A8): Console -> CC -> SM.

The e2e class runs the real chain: a seeded Console app served over
ASGI (CC pulls exactly as in production, credentials included), a real
SM gRPC server with a DirectiveService, and the sync loop in between.
Agent-side execution of skill_install directives is covered by R5-1
tests.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport

from harkeniq_cc.config import CCConfig
from harkeniq_cc.db.repos import AuditRepo, SiteRepo, SkillDeliveryRepo
from harkeniq_cc.marketplace_sync import MarketplaceSync
from harkeniq_cc.runtime import AppState

TENANT = "test-tenant"

SKILL_YAML = """\
name: synced-skill
version: 1
target: fan
rules:
  - condition: "health == 'Warning'"
    verdict: WARNING
    message: "Fan {name} degraded"
default_verdict: HEALTHY
"""


class FakeSMClient:
    """Records install pushes; configurable failures per endpoint."""

    def __init__(self, fail_endpoints: set[str] | None = None):
        self.pushes: list[dict] = []
        self.fail_endpoints = fail_endpoints or set()

    async def install_skill(self, sm_endpoint, token, tenant_id, site_id,
                            skill_name, skill_version, yaml_content,
                            tier="community", validation_state="tested",
                            issued_by=""):
        self.pushes.append({
            "sm_endpoint": sm_endpoint, "site_id": site_id,
            "skill_name": skill_name, "issued_by": issued_by,
        })
        if sm_endpoint in self.fail_endpoints:
            return {"accepted": False, "queued": 0, "reason": "sm down"}
        return {"accepted": True, "queued": 3, "reason": ""}


async def _console_app_with_install(tier: str = "community"):
    """A Console app seeded with one published skill installed by a
    tenant user; returns (app, install_count_probe)."""
    import httpx as _httpx

    from harkeniq_console.app import create_app
    from harkeniq_console.auth import UserContext
    from harkeniq_console.config import ConsoleConfig
    from harkeniq_console.db.base import (
        create_all, make_engine, make_sessionmaker,
    )
    from harkeniq_console.db.repos import (
        MarketplaceInstallRepo, MarketplaceRepo,
    )
    from harkeniq_console.runtime import AppState as ConsoleState

    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    sessionmaker = make_sessionmaker(engine)
    state = ConsoleState(config=ConsoleConfig(insecure=True), engine=engine,
                         sessionmaker=sessionmaker)
    app = create_app(state)

    async with sessionmaker() as session:
        repo = MarketplaceRepo(session)
        entry = await repo.submit(
            "synced-skill", 1, SKILL_YAML, author_email="dev@x.com",
            target="fan", tier=tier,
        )
        await repo.review(entry, approve=True, reviewer_email="rev@x.com")
        await MarketplaceInstallRepo(session).record(
            tenant_id=TENANT, skill_entry_id=entry.id,
            installed_by="op@tenant.com",
        )
        await session.commit()
    return app, engine


@pytest.fixture
async def cc_state(db):
    config = CCConfig(
        tenant_id=TENANT, insecure=True,
        console_url="http://console.test",
        console_api_key="cc-key",
    )
    state = AppState(config=config, sessionmaker=db)
    async with db() as session:
        await SiteRepo(session).upsert(TENANT, "dc-blr-1", "sm1:50051")
        await SiteRepo(session).upsert(TENANT, "dc-mum-1", "sm2:50051")
        await session.commit()
    return state


class TestMarketplaceSync:
    async def test_pull_and_push_to_all_sites(self, cc_state, db):
        console_app, engine = await _console_app_with_install()
        sm = FakeSMClient()
        sync = MarketplaceSync(
            cc_state, sm_client=sm,
            transport=ASGITransport(app=console_app),
        )
        delivered = await sync.run_cycle()
        assert delivered == 2  # one install x two sites
        assert {p["sm_endpoint"] for p in sm.pushes} == {
            "sm1:50051", "sm2:50051",
        }
        assert all(p["issued_by"].startswith("marketplace:")
                   for p in sm.pushes)
        async with db() as session:
            rows = await SkillDeliveryRepo(session).list_all()
            assert len(rows) == 2
            assert all(r.status == "delivered" for r in rows)
            assert all(r.directives_queued == 3 for r in rows)
            audit = AuditRepo(session)
            actions = [r.action for r in
                       await audit.list_filtered(tenant_id=TENANT)]
            assert actions.count("marketplace.skill.deliver") == 2
            assert (await audit.verify_chain()).valid is True
        await engine.dispose()

    async def test_second_cycle_is_idempotent(self, cc_state, db):
        console_app, engine = await _console_app_with_install()
        sm = FakeSMClient()
        sync = MarketplaceSync(
            cc_state, sm_client=sm,
            transport=ASGITransport(app=console_app),
        )
        await sync.run_cycle()
        pushes_after_first = len(sm.pushes)
        assert await sync.run_cycle() == 0
        assert len(sm.pushes) == pushes_after_first
        await engine.dispose()

    async def test_failed_push_recorded_and_retried(self, cc_state, db):
        console_app, engine = await _console_app_with_install()
        sm = FakeSMClient(fail_endpoints={"sm2:50051"})
        sync = MarketplaceSync(
            cc_state, sm_client=sm,
            transport=ASGITransport(app=console_app),
        )
        assert await sync.run_cycle() == 1
        async with db() as session:
            rows = {r.site_id: r
                    for r in await SkillDeliveryRepo(session).list_all()}
            statuses = sorted(r.status for r in rows.values())
            assert statuses == ["delivered", "failed"]

        # SM recovers -> the failed pair is retried, the delivered is not
        sm.fail_endpoints.clear()
        assert await sync.run_cycle() == 1
        async with db() as session:
            rows = await SkillDeliveryRepo(session).list_all()
            assert all(r.status == "delivered" for r in rows)
        await engine.dispose()

    async def test_console_unreachable_is_quiet(self, cc_state):
        sync = MarketplaceSync(cc_state, sm_client=FakeSMClient())
        # console_url points nowhere reachable; cycle returns 0, no raise
        assert await sync.run_cycle() == 0

    async def test_disabled_without_console_url(self, db):
        state = AppState(
            config=CCConfig(tenant_id=TENANT, insecure=True),
            sessionmaker=db,
        )
        sync = MarketplaceSync(state, sm_client=FakeSMClient())
        assert await sync.run_cycle() == 0


class TestEndToEndConsoleToSM:
    """Real chain: Console (ASGI) -> CC sync -> real SM gRPC ->
    skill_install directives queued for the site's devices."""

    async def test_install_reaches_sm_directive_queue(self, cc_state, db):
        from harkeniq_sm.config import SMConfig
        from harkeniq_sm.db.base import (
            create_all as sm_create_all,
            make_engine as sm_make_engine,
            make_sessionmaker as sm_make_sessionmaker,
        )
        from harkeniq_sm.db.repos import DeviceRepo as SMDeviceRepo
        from harkeniq_sm.db.repos import SiteRepo as SMSiteRepo
        from harkeniq_sm.directives import DirectiveService
        from harkeniq_sm.grpc_server import (
            AgentServiceServicer,
            SiteManagerServiceServicer,
            build_server,
        )
        from harkeniq_sm.ingest import IngestService

        # Real SM with two registered devices
        sm_engine = sm_make_engine("sqlite+aiosqlite:///:memory:")
        await sm_create_all(sm_engine)
        sm_db = sm_make_sessionmaker(sm_engine)
        sm_config = SMConfig(insecure=True, site_name="dc-blr-1",
                             grpc_host="127.0.0.1", grpc_port=0)
        directives = DirectiveService(sm_db, sm_config)
        async with sm_db() as session:
            site = await SMSiteRepo(session).get_or_create("dc-blr-1")
            for i in range(2):
                await SMDeviceRepo(session).upsert_registration(
                    site_id=site.id, agent_id=f"agent-{i}", vendor="dell",
                )
            await session.commit()
        server, port = build_server(
            sm_config,
            AgentServiceServicer(IngestService(sm_db, sm_config),
                                 directives=directives),
            sm_servicer=SiteManagerServiceServicer(
                sm_db, None, sm_config, directives=directives,
            ),
        )
        await server.start()

        try:
            # Point CC's single site at the real SM
            async with db() as session:
                site_repo = SiteRepo(session)
                for site in await site_repo.list_all(TENANT):
                    site.sm_endpoint = f"127.0.0.1:{port}"
                await session.commit()

            console_app, console_engine = await _console_app_with_install()
            sync = MarketplaceSync(
                cc_state, transport=ASGITransport(app=console_app),
            )
            delivered = await sync.run_cycle()
            assert delivered == 2  # both CC sites point at the same SM

            # Directives are waiting for both agents
            for agent_id in ("agent-0", "agent-1"):
                queued = await directives.poll(agent_id)
                assert len(queued) >= 1
                assert queued[0].kind == "skill_install"
                assert queued[0].skill_id == "synced-skill"
                assert "marketplace:" in queued[0].issued_by
            await console_engine.dispose()
        finally:
            await server.stop(grace=None)
            await sm_engine.dispose()
