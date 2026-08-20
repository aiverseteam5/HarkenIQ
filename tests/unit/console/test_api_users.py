"""User and RBAC API endpoint tests via httpx.AsyncClient."""

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


class TestInviteUser:
    async def test_invite_user(self, client, tenant_id):
        resp = await client.post(
            f"/api/tenants/{tenant_id}/users/invite",
            json={"email": "alice@acme.com", "role": "operator"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["email"] == "alice@acme.com"
        assert data["role"] == "operator"
        assert data["status"] == "invited"
        assert data["tenant_id"] == tenant_id

    async def test_invite_duplicate_409(self, client, tenant_id):
        await client.post(
            f"/api/tenants/{tenant_id}/users/invite",
            json={"email": "alice@acme.com", "role": "viewer"},
        )
        resp = await client.post(
            f"/api/tenants/{tenant_id}/users/invite",
            json={"email": "alice@acme.com", "role": "operator"},
        )
        assert resp.status_code == 409

    async def test_invite_invalid_role(self, client, tenant_id):
        resp = await client.post(
            f"/api/tenants/{tenant_id}/users/invite",
            json={"email": "bob@acme.com", "role": "nonexistent_role_xyz"},
        )
        assert resp.status_code == 400

    async def test_invite_default_role(self, client, tenant_id):
        resp = await client.post(
            f"/api/tenants/{tenant_id}/users/invite",
            json={"email": "viewer@acme.com"},
        )
        data = resp.json()
        assert data["role"] == "viewer"


class TestListUsers:
    async def test_list_users(self, client, tenant_id):
        await client.post(
            f"/api/tenants/{tenant_id}/users/invite",
            json={"email": "alice@acme.com", "role": "operator"},
        )
        resp = await client.get(f"/api/tenants/{tenant_id}/users/")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["email"] == "alice@acme.com"

    async def test_list_users_empty(self, client, tenant_id):
        resp = await client.get(f"/api/tenants/{tenant_id}/users/")
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []

    async def test_list_users_search(self, client, tenant_id):
        await client.post(
            f"/api/tenants/{tenant_id}/users/invite",
            json={"email": "alice@acme.com", "role": "operator"},
        )
        await client.post(
            f"/api/tenants/{tenant_id}/users/invite",
            json={"email": "bob@globex.com", "role": "viewer"},
        )
        resp = await client.get(
            f"/api/tenants/{tenant_id}/users/?search=alice",
        )
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["email"] == "alice@acme.com"

    async def test_list_users_filter_role(self, client, tenant_id):
        await client.post(
            f"/api/tenants/{tenant_id}/users/invite",
            json={"email": "alice@acme.com", "role": "operator"},
        )
        await client.post(
            f"/api/tenants/{tenant_id}/users/invite",
            json={"email": "bob@acme.com", "role": "viewer"},
        )
        resp = await client.get(
            f"/api/tenants/{tenant_id}/users/?role=operator",
        )
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["role"] == "operator"

    async def test_list_users_pagination(self, client, tenant_id):
        for i in range(5):
            await client.post(
                f"/api/tenants/{tenant_id}/users/invite",
                json={"email": f"user{i}@acme.com", "role": "viewer"},
            )
        resp = await client.get(
            f"/api/tenants/{tenant_id}/users/?page=1&page_size=2",
        )
        data = resp.json()
        assert len(data["items"]) == 2
        assert data["total"] == 5


class TestGetUser:
    async def test_get_user_detail(self, client, tenant_id):
        r = await client.post(
            f"/api/tenants/{tenant_id}/users/invite",
            json={"email": "alice@acme.com", "role": "operator"},
        )
        user_id = r.json()["id"]
        resp = await client.get(
            f"/api/tenants/{tenant_id}/users/{user_id}",
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["email"] == "alice@acme.com"
        assert "effective_permissions" in data
        assert isinstance(data["effective_permissions"], list)
        assert "custom_roles" in data

    async def test_get_user_not_found(self, client, tenant_id):
        resp = await client.get(
            f"/api/tenants/{tenant_id}/users/bad-id",
        )
        assert resp.status_code == 404


class TestUpdateUser:
    async def test_update_user_role(self, client, tenant_id):
        r = await client.post(
            f"/api/tenants/{tenant_id}/users/invite",
            json={"email": "alice@acme.com", "role": "viewer"},
        )
        user_id = r.json()["id"]
        resp = await client.patch(
            f"/api/tenants/{tenant_id}/users/{user_id}",
            json={"role": "operator"},
        )
        assert resp.status_code == 200
        assert resp.json()["role"] == "operator"

    async def test_update_user_display_name(self, client, tenant_id):
        r = await client.post(
            f"/api/tenants/{tenant_id}/users/invite",
            json={"email": "alice@acme.com", "role": "viewer"},
        )
        user_id = r.json()["id"]
        resp = await client.patch(
            f"/api/tenants/{tenant_id}/users/{user_id}",
            json={"display_name": "Alice Smith"},
        )
        assert resp.status_code == 200

    async def test_update_user_not_found(self, client, tenant_id):
        resp = await client.patch(
            f"/api/tenants/{tenant_id}/users/bad-id",
            json={"role": "viewer"},
        )
        assert resp.status_code == 404


class TestDisableUser:
    async def test_disable_user(self, client, tenant_id):
        r = await client.post(
            f"/api/tenants/{tenant_id}/users/invite",
            json={"email": "alice@acme.com", "role": "viewer"},
        )
        user_id = r.json()["id"]
        resp = await client.delete(
            f"/api/tenants/{tenant_id}/users/{user_id}",
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "disabled"
        assert data["id"] == user_id

    async def test_disable_user_not_found(self, client, tenant_id):
        resp = await client.delete(
            f"/api/tenants/{tenant_id}/users/bad-id",
        )
        assert resp.status_code == 404


class TestListPermissions:
    async def test_list_permissions(self, client, tenant_id):
        resp = await client.get(
            f"/api/tenants/{tenant_id}/users/permissions/list",
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "permissions" in data
        assert "roles" in data
        assert len(data["permissions"]) > 0
        # Each permission has key and description
        perm = data["permissions"][0]
        assert "key" in perm
        assert "description" in perm
        # Roles contain at least the fixed ones
        assert "platform_super_admin" in data["roles"]
        assert "viewer" in data["roles"]
