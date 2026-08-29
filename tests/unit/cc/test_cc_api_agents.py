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


class TestAgentControlRemoved:
    """P0 2026-08-29: the enable/disable placebos are GONE. They audited
    and returned "acknowledged" without changing anything anywhere; a
    control that lies about success is removed until a real path exists."""

    async def test_enable_endpoint_removed(self, client):
        r = await client.post("/api/agents/agent-01/enable")
        assert r.status_code in (404, 405)

    async def test_disable_endpoint_removed(self, client):
        r = await client.post("/api/agents/agent-02/disable")
        assert r.status_code in (404, 405)
