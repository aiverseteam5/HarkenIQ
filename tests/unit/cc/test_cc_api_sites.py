"""Sites API endpoint tests against in-memory CC database.

register_site calls SMClient.register_site via gRPC which will fail in
unit tests; we mock SMClient to isolate API logic.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

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
    """FastAPI test client with seeded sites."""
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
        await site_repo.upsert(TENANT, "dc-mum-1", "https://sm2.lab:50051")

        # Add devices to first site for detail test
        cache = FleetCacheRepo(session)
        await cache.upsert_device(site.id, "agent-01", agent_name="srv-01")
        await cache.upsert_device(site.id, "agent-02", agent_name="srv-02")
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, sessionmaker
    await engine.dispose()


@pytest.fixture
async def empty_client():
    """FastAPI test client with no sites."""
    config = CCConfig(tenant_id=TENANT, insecure=True)
    configure_auth("", "", "", insecure=True)
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    sessionmaker = make_sessionmaker(engine)
    state = AppState(config=config, engine=engine, sessionmaker=sessionmaker)
    app = create_app(state)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await engine.dispose()


class TestSiteRegistration:
    @patch("harkeniq_cc.api.sites.SMClient")
    async def test_register_site(self, mock_sm_cls, empty_client):
        mock_instance = AsyncMock()
        mock_instance.register_site.return_value = {
            "accepted": True, "site_token": "tok-123", "reason": "",
        }
        mock_sm_cls.return_value = mock_instance

        r = await empty_client.post(
            "/api/sites/register",
            json={
                "site_name": "dc-new-1",
                "sm_endpoint": "https://sm-new.lab:50051",
                "license_fingerprint": "fp-abc",
            },
        )
        assert r.status_code == 200
        data = r.json()
        assert data["registered"] is True
        assert data["site"]["site_name"] == "dc-new-1"
        assert data["sm_registration"]["accepted"] is True

    @patch("harkeniq_cc.api.sites.SMClient")
    async def test_register_duplicate(self, mock_sm_cls, client):
        # QA-037: only a FULLY registered site (token held) is a duplicate.
        c, sessionmaker = client
        async with sessionmaker() as session:
            site = await SiteRepo(session).get_by_name(TENANT, "dc-blr-1")
            site.sm_token = "already-registered"
            await session.commit()
        r = await c.post(
            "/api/sites/register",
            json={
                "site_name": "dc-blr-1",
                "sm_endpoint": "https://sm.lab:50051",
            },
        )
        assert r.status_code == 409

    @patch("harkeniq_cc.api.sites.SMClient")
    async def test_register_heals_half_registration(self, mock_sm_cls, client):
        # QA-037: a row created while the RegisterSite RPC failed (no
        # sm_token) must be healable by re-running registration — it 409'd
        # forever before, making the seed script unable to recover.
        mock_instance = AsyncMock()
        mock_instance.register_site.return_value = {
            "accepted": True, "site_token": "tok-healed", "reason": "",
        }
        mock_sm_cls.return_value = mock_instance

        c, sessionmaker = client
        r = await c.post(
            "/api/sites/register",
            json={
                "site_name": "dc-blr-1",
                "sm_endpoint": "https://sm.lab:50051",
            },
        )
        assert r.status_code == 200
        assert r.json()["sm_registration"]["accepted"] is True
        async with sessionmaker() as session:
            site = await SiteRepo(session).get_by_name(TENANT, "dc-blr-1")
            assert site.sm_token == "tok-healed"


class TestSiteList:
    async def test_list_sites(self, client):
        c, _ = client
        r = await c.get("/api/sites/")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 2
        names = {s["site_name"] for s in data["sites"]}
        assert "dc-blr-1" in names
        assert "dc-mum-1" in names

    async def test_list_sites_empty(self, empty_client):
        r = await empty_client.get("/api/sites/")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 0
        assert data["sites"] == []

    async def test_list_sites_paginated(self, client):
        c, _ = client
        r = await c.get("/api/sites/", params={"page_size": 1, "page": 1})
        assert r.status_code == 200
        data = r.json()
        assert len(data["sites"]) == 1
        assert data["total"] == 2


class TestSiteDetail:
    async def test_get_site_detail(self, client):
        c, _ = client
        # Get site id
        sites_r = await c.get("/api/sites/")
        site_id = sites_r.json()["sites"][0]["id"]

        r = await c.get(f"/api/sites/{site_id}")
        assert r.status_code == 200
        data = r.json()
        assert data["id"] == site_id
        assert data["device_count"] == 2

    async def test_get_site_not_found(self, client):
        c, _ = client
        r = await c.get("/api/sites/nonexistent-id")
        assert r.status_code == 404
