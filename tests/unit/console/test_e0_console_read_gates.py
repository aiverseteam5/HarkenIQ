"""E0.3: the Console's role-bundle listing, and /metrics.

A13 follow-up (a): listing which permission bundles exist in a tenant
was gated on `role.manage`, a MUTATION permission, so an auditor could
not see the roles they were auditing. Creating, editing and deleting a
role are unchanged.
"""

from __future__ import annotations

import httpx
import pytest

from harkeniq_console.api.deps import get_current_user
from harkeniq_console.app import create_app
from harkeniq_console.auth import UserContext
from harkeniq_console.config import ConsoleConfig
from harkeniq_console.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_console.db.models import CustomRole, Tenant
from harkeniq_console.permissions import ROLE_PERMISSIONS
from harkeniq_console.runtime import AppState

TENANT = "tenant-1"


async def _client(role: str):
    config = ConsoleConfig(insecure=True)
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    sessionmaker = make_sessionmaker(engine)
    state = AppState(config=config, engine=engine, sessionmaker=sessionmaker)
    app = create_app(state)

    async with sessionmaker() as session:
        session.add(Tenant(id=TENANT, slug="t1", name="Tenant One"))
        session.add(CustomRole(
            tenant_id=TENANT, name="branch-operator",
            permissions=["fleet.view", "action.approve"], created_by="owner",
        ))
        await session.commit()

    async def _fake():
        return UserContext(
            user_id=f"kc-{role}", email=f"{role}@example.com",
            tenant_id=TENANT, role=role,
            permissions=sorted(ROLE_PERMISSIONS.get(role, set())),
            is_platform_user=False,
        )

    app.dependency_overrides[get_current_user] = _fake
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://console",
    )


class TestRoleBundleListing:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("role", ["auditor", "tenant_owner", "site_admin"])
    async def test_user_view_holders_can_list_role_bundles(self, role):
        client = await _client(role)
        res = await client.get(f"/api/tenants/{TENANT}/roles/")
        assert res.status_code == 200, f"{role} cannot see the roles it audits"
        assert any(r["name"] == "branch-operator" for r in res.json())
        await client.aclose()

    @pytest.mark.asyncio
    async def test_a_viewer_without_user_view_is_still_refused(self):
        client = await _client("viewer")
        assert (
            await client.get(f"/api/tenants/{TENANT}/roles/")
        ).status_code == 403
        await client.aclose()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("role", ["auditor", "operator"])
    async def test_role_mutation_still_needs_role_manage(self, role):
        """Opening the read must not open the write."""
        client = await _client(role)
        res = await client.post(
            f"/api/tenants/{TENANT}/roles/",
            json={"name": "invented", "permissions": ["fleet.view"]},
        )
        assert res.status_code == 403
        await client.aclose()


class TestConsoleMetrics:
    @pytest.mark.asyncio
    async def test_metrics_is_served(self):
        client = await _client("tenant_owner")
        res = await client.get("/metrics")
        assert res.status_code == 200
        assert "harkeniq_up 1.0" in res.text
        await client.aclose()
