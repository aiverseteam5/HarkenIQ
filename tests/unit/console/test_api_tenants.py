"""Tenant API endpoint tests via httpx.AsyncClient."""

import pytest
import httpx

from harkeniq_console.app import create_app
from harkeniq_console.config import ConsoleConfig
from harkeniq_console.runtime import AppState
from harkeniq_console.db.base import create_all, make_engine, make_sessionmaker


@pytest.fixture
async def client():
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    sm = make_sessionmaker(engine)
    config = ConsoleConfig(insecure=True)
    state = AppState(config=config, engine=engine, sessionmaker=sm)
    app = create_app(state)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as c:
        yield c
    await engine.dispose()


def _create_body(**kwargs) -> dict:
    defaults = {
        "name": "Acme Corp",
        "slug": "acme",
        "billing_country": "US",
        "currency": "USD",
        "plan": "approve",
        "node_commit": 100,
        "admin_email": "",
    }
    defaults.update(kwargs)
    return defaults


class TestHealthz:
    async def test_healthz(self, client):
        resp = await client.get("/healthz")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["service"] == "console"


class TestCreateTenant:
    async def test_create_tenant_201(self, client):
        resp = await client.post(
            "/api/admin/tenants/",
            json=_create_body(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["slug"] == "acme"
        assert data["name"] == "Acme Corp"
        assert data["status"] == "active"
        assert "id" in data
        assert "created_at" in data

    async def test_create_tenant_slug_conflict_409(self, client):
        await client.post("/api/admin/tenants/", json=_create_body())
        resp = await client.post("/api/admin/tenants/", json=_create_body())
        assert resp.status_code == 409

    async def test_create_tenant_with_owner(self, client):
        resp = await client.post(
            "/api/admin/tenants/",
            json=_create_body(admin_email="owner@acme.com"),
        )
        data = resp.json()
        assert data["owner_email"] == "owner@acme.com"
        assert data["owner_id"] is not None


class TestListTenants:
    async def test_list_tenants_empty(self, client):
        resp = await client.get("/api/admin/tenants/")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 0

    async def test_list_tenants_after_create(self, client):
        await client.post("/api/admin/tenants/", json=_create_body())
        resp = await client.get("/api/admin/tenants/")
        data = resp.json()
        assert data["total"] == 1
        assert len(data["items"]) == 1

    async def test_list_tenants_pagination(self, client):
        for i in range(5):
            await client.post(
                "/api/admin/tenants/",
                json=_create_body(name=f"Tenant {i}", slug=f"tenant-{i}"),
            )
        resp = await client.get("/api/admin/tenants/?page=1&page_size=2")
        data = resp.json()
        assert len(data["items"]) == 2
        assert data["total"] == 5
        assert data["page"] == 1
        assert data["page_size"] == 2

    async def test_list_tenants_search(self, client):
        await client.post(
            "/api/admin/tenants/", json=_create_body(name="Acme Corp", slug="acme"),
        )
        await client.post(
            "/api/admin/tenants/", json=_create_body(name="Globex Inc", slug="globex"),
        )
        resp = await client.get("/api/admin/tenants/?search=Acme")
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["name"] == "Acme Corp"

    async def test_list_tenants_filter_status(self, client):
        r = await client.post("/api/admin/tenants/", json=_create_body())
        tid = r.json()["id"]
        await client.post(
            f"/api/admin/tenants/{tid}/suspend",
            json={"reason": "billing"},
        )
        resp_active = await client.get("/api/admin/tenants/?status=active")
        resp_susp = await client.get("/api/admin/tenants/?status=suspended")
        assert resp_active.json()["total"] == 0
        assert resp_susp.json()["total"] == 1


class TestGetTenant:
    async def test_get_tenant_detail(self, client):
        r = await client.post("/api/admin/tenants/", json=_create_body())
        tid = r.json()["id"]
        resp = await client.get(f"/api/admin/tenants/{tid}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["slug"] == "acme"
        assert "user_count" in data
        assert "billing_country" in data

    async def test_get_tenant_not_found_404(self, client):
        resp = await client.get("/api/admin/tenants/nonexistent-id")
        assert resp.status_code == 404


class TestUpdateTenant:
    async def test_update_tenant(self, client):
        r = await client.post("/api/admin/tenants/", json=_create_body())
        tid = r.json()["id"]
        resp = await client.patch(
            f"/api/admin/tenants/{tid}",
            json={"name": "Acme Industries"},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "Acme Industries"

    async def test_update_tenant_not_found(self, client):
        resp = await client.patch(
            "/api/admin/tenants/bad-id",
            json={"name": "New Name"},
        )
        assert resp.status_code == 404


class TestSuspendReactivate:
    async def test_suspend_tenant(self, client):
        r = await client.post("/api/admin/tenants/", json=_create_body())
        tid = r.json()["id"]
        resp = await client.post(
            f"/api/admin/tenants/{tid}/suspend",
            json={"reason": "non-payment"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "suspended"

    async def test_reactivate_tenant(self, client):
        r = await client.post("/api/admin/tenants/", json=_create_body())
        tid = r.json()["id"]
        await client.post(
            f"/api/admin/tenants/{tid}/suspend",
            json={"reason": "billing"},
        )
        resp = await client.post(f"/api/admin/tenants/{tid}/reactivate")
        assert resp.status_code == 200
        assert resp.json()["status"] == "active"

    async def test_suspend_not_found(self, client):
        resp = await client.post(
            "/api/admin/tenants/bad-id/suspend",
            json={"reason": "test"},
        )
        assert resp.status_code == 404

    async def test_reactivate_not_suspended(self, client):
        r = await client.post("/api/admin/tenants/", json=_create_body())
        tid = r.json()["id"]
        resp = await client.post(f"/api/admin/tenants/{tid}/reactivate")
        assert resp.status_code == 409
