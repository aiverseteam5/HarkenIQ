"""MockKeycloakAdminClient behavior tests."""

import pytest

from harkeniq_console.keycloak_admin import (
    DEFAULT_REALM_ROLES,
    KeycloakError,
    MockKeycloakAdminClient,
)


@pytest.fixture
def kc():
    return MockKeycloakAdminClient()


class TestMockKeycloakAdmin:
    async def test_get_admin_token(self, kc):
        token = await kc.get_admin_token()
        assert isinstance(token, str)
        assert len(token) > 0

    async def test_create_realm(self, kc):
        realm = await kc.create_realm("acme")
        assert realm == "acme"
        assert "acme" in kc._realms
        # Default roles provisioned
        assert set(DEFAULT_REALM_ROLES).issubset(set(kc._realm_roles["acme"]))

    async def test_create_realm_duplicate(self, kc):
        await kc.create_realm("acme")
        with pytest.raises(KeycloakError) as exc_info:
            await kc.create_realm("acme")
        assert exc_info.value.status_code == 409

    async def test_create_client(self, kc):
        await kc.create_realm("acme")
        client_uuid = await kc.create_client(
            "acme", "harkeniq-console", ["http://localhost:8100/*"],
        )
        assert len(client_uuid) > 0
        assert client_uuid in kc._clients["acme"]

    async def test_create_client_realm_not_found(self, kc):
        with pytest.raises(KeycloakError) as exc_info:
            await kc.create_client("missing", "c", [])
        assert exc_info.value.status_code == 404

    async def test_create_user(self, kc):
        await kc.create_realm("acme")
        user_id = await kc.create_user("acme", "alice@acme.com")
        assert len(user_id) > 0
        assert user_id in kc._users["acme"]
        assert kc._users["acme"][user_id]["email"] == "alice@acme.com"

    async def test_create_user_duplicate(self, kc):
        await kc.create_realm("acme")
        await kc.create_user("acme", "alice@acme.com")
        with pytest.raises(KeycloakError) as exc_info:
            await kc.create_user("acme", "alice@acme.com")
        assert exc_info.value.status_code == 409

    async def test_assign_role(self, kc):
        await kc.create_realm("acme")
        user_id = await kc.create_user("acme", "alice@acme.com")
        await kc.assign_realm_role("acme", user_id, "tenant_owner")
        assert "tenant_owner" in kc._role_mappings[("acme", user_id)]

    async def test_assign_role_unknown_role(self, kc):
        await kc.create_realm("acme")
        user_id = await kc.create_user("acme", "alice@acme.com")
        with pytest.raises(KeycloakError) as exc_info:
            await kc.assign_realm_role("acme", user_id, "nonexistent_role")
        assert exc_info.value.status_code == 404

    async def test_delete_user(self, kc):
        await kc.create_realm("acme")
        user_id = await kc.create_user("acme", "alice@acme.com")
        await kc.delete_user("acme", user_id)
        assert user_id not in kc._users["acme"]

    async def test_list_users(self, kc):
        await kc.create_realm("acme")
        await kc.create_user("acme", "alice@acme.com")
        await kc.create_user("acme", "bob@acme.com")
        users = await kc.list_realm_users("acme")
        assert len(users) == 2
        emails = {u["email"] for u in users}
        assert emails == {"alice@acme.com", "bob@acme.com"}

    async def test_delete_realm(self, kc):
        await kc.create_realm("acme")
        await kc.create_user("acme", "alice@acme.com")
        await kc.delete_realm("acme")
        assert "acme" not in kc._realms
        assert "acme" not in kc._users

    async def test_delete_realm_not_found(self, kc):
        with pytest.raises(KeycloakError) as exc_info:
            await kc.delete_realm("missing")
        assert exc_info.value.status_code == 404


class TestGetUserAuthority:
    """A30.23: the read Central Command's dispatch gate stands on."""

    async def test_mock_reports_enabled_and_sorted_roles(self, kc):
        await kc.create_realm("acme")
        uid = await kc.create_user("acme", "op@acme.com")
        await kc.assign_realm_role("acme", uid, "operator")
        await kc.assign_realm_role("acme", uid, "auditor")
        assert await kc.get_user_authority("acme", uid) == {
            "enabled": True, "realm_roles": ["auditor", "operator"],
        }

    async def test_mock_absent_user_is_none(self, kc):
        await kc.create_realm("acme")
        assert await kc.get_user_authority("acme", "nobody") is None

    async def test_mock_unknown_realm_raises(self, kc):
        with pytest.raises(KeycloakError):
            await kc.get_user_authority("nope", "x")

    async def test_real_client_asks_the_user_record_and_the_composite_mapping(self):
        """The real client's two admin calls and how it reads them: the
        user record for `enabled`, the COMPOSITE realm mapping for the
        effective roles (what a token's realm_access.roles carries), an
        absent user as None, and any other failure re-raised."""
        from harkeniq_console.keycloak_admin import KeycloakAdminClient

        client = KeycloakAdminClient("http://kc", "admin", "pw")
        calls: list[str] = []

        class _Resp:
            def __init__(self, payload):
                self._payload = payload

            def json(self):
                return self._payload

        async def _request(method, path, *, json=None, context=""):
            calls.append(f"{method} {path}")
            if path.endswith("/role-mappings/realm/composite"):
                return _Resp([{"id": "1", "name": "operator"}, {"id": "2", "name": "auditor"},
                              {"id": "3"}])
            if path.endswith("/users/gone"):
                raise KeycloakError("get user 'gone' not found", status_code=404)
            if path.endswith("/users/broken"):
                raise KeycloakError("admin authentication failed", status_code=401)
            return _Resp({"id": "u1", "enabled": True})

        client._request = _request  # type: ignore[method-assign]
        assert await client.get_user_authority("acme", "u 1") == {
            "enabled": True, "realm_roles": ["auditor", "operator"],
        }
        assert calls == [
            "GET /admin/realms/acme/users/u%201",
            "GET /admin/realms/acme/users/u%201/role-mappings/realm/composite",
        ]
        assert await client.get_user_authority("acme", "gone") is None
        with pytest.raises(KeycloakError):
            await client.get_user_authority("acme", "broken")
