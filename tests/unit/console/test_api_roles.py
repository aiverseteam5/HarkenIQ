"""Custom role API endpoint tests via httpx.AsyncClient."""

import pytest
import httpx

from harkeniq_console.app import create_app
from harkeniq_console.config import ConsoleConfig
from harkeniq_console.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_console.runtime import AppState


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


@pytest.fixture
async def tenant_id(client):
    resp = await client.post(
        "/api/admin/tenants/",
        json={
            "name": "Acme Corp",
            "slug": "acme",
            "billing_country": "US",
        },
    )
    return resp.json()["id"]


class TestCreateRole:
    async def test_create_role(self, client, tenant_id):
        resp = await client.post(
            f"/api/tenants/{tenant_id}/roles/",
            json={
                "name": "shift-lead",
                "permissions": ["fleet.view", "incident.view"],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "shift-lead"
        assert set(data["permissions"]) == {"fleet.view", "incident.view"}
        assert data["tenant_id"] == tenant_id
        assert "id" in data
        assert "created_at" in data

    async def test_create_role_exceeds_ceiling_400(self, client, tenant_id):
        # admin.dashboard is only for platform_super_admin, not tenant_owner
        resp = await client.post(
            f"/api/tenants/{tenant_id}/roles/",
            json={
                "name": "overreach",
                "permissions": ["admin.dashboard"],
            },
        )
        assert resp.status_code == 400
        assert "ceiling" in resp.json()["detail"]

    async def test_create_role_empty_permissions(self, client, tenant_id):
        resp = await client.post(
            f"/api/tenants/{tenant_id}/roles/",
            json={"name": "empty-role", "permissions": []},
        )
        assert resp.status_code == 200
        assert resp.json()["permissions"] == []

    async def test_create_role_valid_tenant_owner_perms(self, client, tenant_id):
        """All permissions within tenant_owner ceiling should succeed."""
        resp = await client.post(
            f"/api/tenants/{tenant_id}/roles/",
            json={
                "name": "full-ops",
                "permissions": [
                    "fleet.view", "action.approve", "incident.view",
                    "incident.acknowledge", "support.create", "support.view",
                ],
            },
        )
        assert resp.status_code == 200


class TestListRoles:
    async def test_list_roles_empty(self, client, tenant_id):
        resp = await client.get(f"/api/tenants/{tenant_id}/roles/")
        assert resp.status_code == 200
        data = resp.json()
        assert data == []

    async def test_list_roles_after_create(self, client, tenant_id):
        await client.post(
            f"/api/tenants/{tenant_id}/roles/",
            json={"name": "role-a", "permissions": ["fleet.view"]},
        )
        await client.post(
            f"/api/tenants/{tenant_id}/roles/",
            json={"name": "role-b", "permissions": ["incident.view"]},
        )
        resp = await client.get(f"/api/tenants/{tenant_id}/roles/")
        data = resp.json()
        assert len(data) == 2
        names = {r["name"] for r in data}
        assert names == {"role-a", "role-b"}


class TestUpdateRole:
    async def test_update_role(self, client, tenant_id):
        r = await client.post(
            f"/api/tenants/{tenant_id}/roles/",
            json={"name": "shift-lead", "permissions": ["fleet.view"]},
        )
        role_id = r.json()["id"]
        resp = await client.patch(
            f"/api/tenants/{tenant_id}/roles/{role_id}",
            json={"permissions": ["fleet.view", "action.approve"]},
        )
        assert resp.status_code == 200
        assert set(resp.json()["permissions"]) == {"fleet.view", "action.approve"}

    async def test_update_role_name(self, client, tenant_id):
        r = await client.post(
            f"/api/tenants/{tenant_id}/roles/",
            json={"name": "old-name", "permissions": ["fleet.view"]},
        )
        role_id = r.json()["id"]
        resp = await client.patch(
            f"/api/tenants/{tenant_id}/roles/{role_id}",
            json={"name": "new-name"},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "new-name"

    async def test_update_role_not_found(self, client, tenant_id):
        resp = await client.patch(
            f"/api/tenants/{tenant_id}/roles/bad-id",
            json={"name": "x"},
        )
        assert resp.status_code == 404

    async def test_update_role_exceeds_ceiling(self, client, tenant_id):
        r = await client.post(
            f"/api/tenants/{tenant_id}/roles/",
            json={"name": "limited", "permissions": ["fleet.view"]},
        )
        role_id = r.json()["id"]
        resp = await client.patch(
            f"/api/tenants/{tenant_id}/roles/{role_id}",
            json={"permissions": ["admin.dashboard"]},
        )
        assert resp.status_code == 400


class TestDeleteRole:
    async def test_delete_role(self, client, tenant_id):
        r = await client.post(
            f"/api/tenants/{tenant_id}/roles/",
            json={"name": "temp-role", "permissions": []},
        )
        role_id = r.json()["id"]
        resp = await client.delete(
            f"/api/tenants/{tenant_id}/roles/{role_id}",
        )
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True
        assert resp.json()["id"] == role_id
        # Confirm it's gone
        list_resp = await client.get(f"/api/tenants/{tenant_id}/roles/")
        assert len(list_resp.json()) == 0

    async def test_delete_role_not_found_404(self, client, tenant_id):
        resp = await client.delete(
            f"/api/tenants/{tenant_id}/roles/bad-id",
        )
        assert resp.status_code == 404
