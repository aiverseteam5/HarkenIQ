"""Fleet API endpoint tests against in-memory CC database."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from harkeniq_cc.app import create_app
from harkeniq_cc.auth import configure_auth
from harkeniq_cc.config import CCConfig
from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_cc.db.repos import FleetCacheRepo, SiteRepo
from harkeniq_cc.runtime import AppState

from tests.unit.cc.conftest import seed_tenant_admin

TENANT = "test-tenant"


@pytest.fixture
async def client():
    """FastAPI test client with in-memory DB, insecure auth, and seeded data."""
    config = CCConfig(tenant_id=TENANT, insecure=True)
    configure_auth("", "", "", insecure=True)
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    sessionmaker = make_sessionmaker(engine)
    state = AppState(config=config, engine=engine, sessionmaker=sessionmaker)
    app = create_app(state)
    # A23-5: a rowless tenant is STRICT now (A23.11), so this
    # fixture seeds the founding administrator that tenant
    # birth seeds (A23.14 D4) instead of leaning on the
    # `legacy_open` synthesis a missing row used to give.
    await seed_tenant_admin(sessionmaker, TENANT, "lab-user")

    # Seed test data
    async with sessionmaker() as session:
        site_repo = SiteRepo(session)
        site = await site_repo.upsert(TENANT, "dc-blr-1", "https://sm1.lab:50051")
        site2 = await site_repo.upsert(TENANT, "dc-mum-1", "https://sm2.lab:50051")

        cache = FleetCacheRepo(session)
        for i in range(10):
            health = "ok" if i < 6 else ("warning" if i < 8 else "critical")
            sid = site.id if i < 7 else site2.id
            await cache.upsert_device(
                site_id=sid,
                agent_id=f"agent-{i:02d}",
                agent_name=f"srv-{i:02d}",
                vendor="Dell" if i % 2 == 0 else "HPE",
                model="R750" if i % 2 == 0 else "DL380",
                observation="observed",
                health=health,
            )
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    await engine.dispose()


@pytest.fixture
async def empty_client():
    """FastAPI test client with empty DB."""
    config = CCConfig(tenant_id=TENANT, insecure=True)
    configure_auth("", "", "", insecure=True)
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    sessionmaker = make_sessionmaker(engine)
    state = AppState(config=config, engine=engine, sessionmaker=sessionmaker)
    app = create_app(state)
    # A23-5: a rowless tenant is STRICT now (A23.11), so this
    # fixture seeds the founding administrator that tenant
    # birth seeds (A23.14 D4) instead of leaning on the
    # `legacy_open` synthesis a missing row used to give.
    await seed_tenant_admin(sessionmaker, TENANT, "lab-user")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await engine.dispose()


class TestFleetList:
    async def test_fleet_list_empty(self, empty_client):
        r = await empty_client.get("/api/fleet/")
        assert r.status_code == 200
        data = r.json()
        assert data["devices"] == []
        assert data["total"] == 0

    async def test_fleet_list_with_data(self, client):
        r = await client.get("/api/fleet/")
        assert r.status_code == 200
        data = r.json()
        assert len(data["devices"]) == 10
        assert data["total"] == 10

    async def test_fleet_list_paginated(self, client):
        r = await client.get("/api/fleet/", params={"page_size": 3, "page": 1})
        assert r.status_code == 200
        data = r.json()
        assert len(data["devices"]) == 3
        assert data["total"] == 10
        assert data["page"] == 1
        assert data["page_size"] == 3

    async def test_fleet_list_page_2(self, client):
        r = await client.get("/api/fleet/", params={"page_size": 3, "page": 2})
        assert r.status_code == 200
        data = r.json()
        assert len(data["devices"]) == 3
        assert data["page"] == 2

    async def test_fleet_list_filter_health(self, client):
        r = await client.get("/api/fleet/", params={"health": "critical"})
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 2
        for d in data["devices"]:
            assert d["health"] == "critical"

    async def test_fleet_list_filter_site(self, client):
        # First get the site id
        sites_r = await client.get("/api/sites/")
        site_id = sites_r.json()["sites"][0]["id"]
        r = await client.get("/api/fleet/", params={"site_id": site_id})
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 7

    async def test_fleet_list_search(self, client):
        r = await client.get("/api/fleet/", params={"search": "agent-01"})
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 1
        assert data["devices"][0]["agent_id"] == "agent-01"

    async def test_fleet_list_search_by_name(self, client):
        r = await client.get("/api/fleet/", params={"search": "srv-05"})
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 1
        assert data["devices"][0]["agent_name"] == "srv-05"


class TestFleetSummary:
    async def test_fleet_summary(self, client):
        r = await client.get("/api/fleet/summary")
        assert r.status_code == 200
        data = r.json()
        assert data["total_nodes"] == 10
        assert data["by_health"]["ok"] == 6
        assert data["by_health"]["warning"] == 2
        assert data["by_health"]["critical"] == 2
        assert data["sites_count"] == 2

    async def test_fleet_summary_empty(self, empty_client):
        r = await empty_client.get("/api/fleet/summary")
        assert r.status_code == 200
        data = r.json()
        assert data["total_nodes"] == 0
        assert data["by_health"]["ok"] == 0
        assert data["by_health"]["warning"] == 0
        assert data["by_health"]["critical"] == 0
        assert data["by_health"]["unknown"] == 0
        assert data["sites_count"] == 0
