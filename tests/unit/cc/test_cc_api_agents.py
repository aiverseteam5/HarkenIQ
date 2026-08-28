"""Agents API endpoint tests against in-memory CC database."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from harkeniq_cc.app import create_app
from harkeniq_cc.auth import configure_auth
from harkeniq_cc.config import CCConfig
from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_cc.db.repos import FleetCacheRepo, SiteRepo
from harkeniq_cc.runtime import AppState

TENANT = "test-tenant"


@pytest.fixture
async def client():
    """FastAPI test client with seeded agents in fleet cache."""
    config = CCConfig(tenant_id=TENANT, insecure=True)
    configure_auth("", "", "", insecure=True)
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    sessionmaker = make_sessionmaker(engine)
    state = AppState(config=config, engine=engine, sessionmaker=sessionmaker)
    app = create_app(state)

    async with sessionmaker() as session:
        site_repo = SiteRepo(session)
        site = await site_repo.upsert(TENANT, "dc-blr-1", "https://sm1.lab:50051")

        cache = FleetCacheRepo(session)
        await cache.upsert_device(
            site.id, "agent-01", agent_name="srv-01",
            vendor="Dell", model="R750", observation="observed", health="ok",
        )
        await cache.upsert_device(
            site.id, "agent-02", agent_name="srv-02",
            vendor="HPE", model="DL380", observation="observed", health="warning",
        )
        await cache.upsert_device(
            site.id, "agent-03", agent_name="srv-03",
            vendor="Dell", model="R750", observation="stale", health="unknown",
        )
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await engine.dispose()


class TestListAgents:
    async def test_list_agents(self, client):
        r = await client.get("/api/agents/")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 3
        assert len(data["agents"]) == 3

    async def test_list_agents_search(self, client):
        r = await client.get("/api/agents/", params={"search": "agent-02"})
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 1
        assert data["agents"][0]["agent_id"] == "agent-02"

    async def test_list_agents_paginated(self, client):
        r = await client.get("/api/agents/", params={"page_size": 2, "page": 1})
        assert r.status_code == 200
        data = r.json()
        assert len(data["agents"]) == 2
        assert data["total"] == 3


class TestAgentDetail:
    async def test_get_agent_detail(self, client):
        r = await client.get("/api/agents/agent-01")
        assert r.status_code == 200
        data = r.json()
        assert data["agent_id"] == "agent-01"
        assert data["agent_name"] == "srv-01"
        assert data["vendor"] == "Dell"
        assert data["site_name"] == "dc-blr-1"

    async def test_get_agent_not_found(self, client):
        r = await client.get("/api/agents/nonexistent")
        assert r.status_code == 404


class TestAgentControl:
    async def test_enable_agent(self, client):
        r = await client.post("/api/agents/agent-01/enable")
        assert r.status_code == 200
        data = r.json()
        assert data["agent_id"] == "agent-01"
        assert data["action"] == "enable"
        assert data["status"] == "acknowledged"

    async def test_disable_agent(self, client):
        r = await client.post("/api/agents/agent-02/disable")
        assert r.status_code == 200
        data = r.json()
        assert data["agent_id"] == "agent-02"
        assert data["action"] == "disable"
        assert data["status"] == "acknowledged"

    async def test_enable_nonexistent(self, client):
        r = await client.post("/api/agents/nonexistent/enable")
        assert r.status_code == 404

    async def test_disable_nonexistent(self, client):
        r = await client.post("/api/agents/nonexistent/disable")
        assert r.status_code == 404


class TestAgentPayloadContract:
    """The Console's Agent type was invented, not derived from this payload.

    AgentManagement.tsx declared version / status / device / enabled /
    last_heartbeat_at and keyed rows off `.id`. CC has never sent any of
    them, so those columns rendered empty and every detail or
    enable/disable request built /api/agents/undefined/... . These pin the
    fields the page actually reads, so the contract cannot drift silently
    again.
    """

    async def test_list_resolves_site_name(self, client):
        # The page renders a site column; CC used to send only a raw UUID,
        # and site_name existed on the detail route alone.
        r = await client.get("/api/agents/")
        assert r.status_code == 200
        for agent in r.json()["agents"]:
            assert agent["site_name"] == "dc-blr-1"

    async def test_list_exposes_every_field_the_page_reads(self, client):
        r = await client.get("/api/agents/")
        agent = r.json()["agents"][0]
        for key in (
            "agent_id", "agent_name", "vendor", "model", "device_class",
            "observation", "health", "site_id", "site_name",
            "last_seen_at", "snapshot_at",
        ):
            assert key in agent, f"{key} missing from /api/agents payload"

    async def test_last_seen_is_null_not_the_cache_refresh_time(self, client):
        # These fixtures were cached without a site reading. Reporting
        # snapshot_at here made a stale agent look fresh on every poll.
        r = await client.get("/api/agents/")
        agent = r.json()["agents"][0]
        assert agent["last_seen_at"] is None
        assert agent["snapshot_at"] is not None

    async def test_detail_is_keyed_by_agent_id(self, client):
        r = await client.get("/api/agents/agent-01")
        assert r.status_code == 200
        assert r.json()["agent_id"] == "agent-01"
        assert r.json()["site_name"] == "dc-blr-1"


class TestAgentFilters:
    """list_filtered has always supported health and vendor; the endpoint
    never forwarded them, so the Console's filter controls did nothing."""

    async def test_health_filter(self, client):
        r = await client.get("/api/agents/?health=warning")
        data = r.json()
        assert data["total"] == 1
        assert data["agents"][0]["agent_id"] == "agent-02"

    async def test_vendor_filter(self, client):
        r = await client.get("/api/agents/?vendor=Dell")
        assert r.json()["total"] == 2

    async def test_filters_compose(self, client):
        r = await client.get("/api/agents/?vendor=Dell&health=ok")
        data = r.json()
        assert data["total"] == 1
        assert data["agents"][0]["agent_id"] == "agent-01"
