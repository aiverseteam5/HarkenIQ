"""CC audit reads require audit.view — not just a valid token.

Review 2026-08-28 (adversarial pass, verified): /api/audit/ and
/api/audit/verify were gated by authentication alone while every other
sensitive CC surface used require_permission, so any authenticated viewer
could read the tenant's audit trail through the Console proxy. Auditor is
the role whose JOB is the audit trail (spec §4 role 6); operator/viewer
hold plain "view" and are refused.
"""

import httpx
import pytest

from harkeniq_cc.api.deps import get_current_user
from harkeniq_cc.app import create_app
from harkeniq_cc.auth import UserContext, configure_auth
from harkeniq_cc.config import CCConfig
from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_cc.runtime import AppState

TENANT = "test-tenant"


def _user(role: str, perms: list[str]) -> UserContext:
    return UserContext(
        user_id=f"kc-{role}", email=f"{role}@example.com", tenant_id=TENANT,
        role=role, permissions=perms,
        is_platform_user=role == "platform_super_admin",
    )


async def _client_as(role: str, perms: list[str]):
    config = CCConfig(tenant_id=TENANT, insecure=True)
    configure_auth("", "", "", insecure=True)
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    state = AppState(
        config=config, engine=engine, sessionmaker=make_sessionmaker(engine),
    )
    app = create_app(state)

    async def _fake():
        return _user(role, perms)

    app.dependency_overrides[get_current_user] = _fake
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    )
    return client, engine


class TestAuditRequiresAuditView:
    @pytest.mark.parametrize("role,perms,expected", [
        # CC's mapping: admins carry "*", auditor carries audit.view,
        # operator/viewer carry plain "view" (the closed hole).
        ("platform_super_admin", ["*"], 200),
        ("tenant_owner", ["*"], 200),
        ("auditor", ["view", "audit.view"], 200),
        ("operator", ["view"], 403),
        ("viewer", ["view"], 403),
    ])
    async def test_audit_list_per_role(self, role, perms, expected):
        client, engine = await _client_as(role, perms)
        try:
            assert (await client.get("/api/audit/")).status_code == expected
            assert (await client.get("/api/audit/verify")).status_code == expected
        finally:
            await client.aclose()
            await engine.dispose()

    async def test_auth_mapping_grants_auditor_audit_view(self):
        """Pin the role->permission mapping itself, so the route gate and
        the mapping cannot drift apart silently."""
        from harkeniq_cc.auth import _ADMIN_ROLES, _EXTRA_PERMISSIONS

        assert "audit.view" in _EXTRA_PERMISSIONS.get("auditor", [])
        assert "auditor" not in _ADMIN_ROLES
