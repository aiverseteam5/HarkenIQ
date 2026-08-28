"""Tenant-plane authorization: permissions, not just membership.

``tenant_scope`` answers "which tenant?" — it never answered "are you
allowed?". Whether a permission was checked at all depended on whether the
individual route happened to call ``has_permission`` in its body, and five
of seven tenant routers did not. The only thing refusing a ``viewer`` who
called them directly was the SPA declining to render the button, which
inverts spec S4: "enforced server-side, not just hidden in the UI".

Concretely, before this module existed a ``viewer`` — who holds exactly
``{fleet.view, incident.view}`` — could mint and revoke tenant API keys,
pay an invoice, and export the tenant audit log.

These tests pin the tenant plane against ROLE_PERMISSIONS from the outside,
per role, so a route can never again quietly ship without a permission.
"""

import httpx
import pytest

from harkeniq_console.app import create_app
from harkeniq_console.auth import UserContext, get_current_user
from harkeniq_console.config import ConsoleConfig
from harkeniq_console.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_console.db.models import Tenant
from harkeniq_console.runtime import AppState


async def _client_as(role: str, *, platform: bool = False):
    """Client authenticated as *role* against a tenant that exists."""
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    sm = make_sessionmaker(engine)
    async with sm() as session:
        tenant = Tenant(name="Acme", slug="acme", billing_country="US")
        session.add(tenant)
        await session.flush()
        tenant_id = tenant.id
        await session.commit()

    config = ConsoleConfig(insecure=False)
    state = AppState(config=config, engine=engine, sessionmaker=sm)
    app = create_app(state)

    async def _fake_user() -> UserContext:
        return UserContext(
            user_id="kc-sub-1",
            email=f"{role}@example.com",
            tenant_id=None if platform else tenant_id,
            role=role,
            permissions=[],
            is_platform_user=platform,
        )

    app.dependency_overrides[get_current_user] = _fake_user
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
        follow_redirects=True,
    )
    return client, engine, tenant_id


class TestViewerCannotEscalate:
    """A viewer holds fleet.view and incident.view. Nothing else."""

    async def test_viewer_cannot_mint_an_api_key(self):
        """The sharpest hole: a read-only user minting tenant credentials."""
        client, engine, tenant_id = await _client_as("viewer")
        try:
            resp = await client.post(
                f"/api/tenants/{tenant_id}/api-keys/",
                json={"name": "escalation", "scope": "read"},
            )
            assert resp.status_code == 403, (
                f"viewer minted an API key: {resp.status_code} {resp.text}"
            )
        finally:
            await client.aclose()
            await engine.dispose()

    async def test_viewer_cannot_list_api_keys(self):
        client, engine, tenant_id = await _client_as("viewer")
        try:
            resp = await client.get(f"/api/tenants/{tenant_id}/api-keys/")
            assert resp.status_code == 403
        finally:
            await client.aclose()
            await engine.dispose()

    async def test_viewer_cannot_read_audit_log(self):
        client, engine, tenant_id = await _client_as("viewer")
        try:
            resp = await client.get(f"/api/tenants/{tenant_id}/audit/")
            assert resp.status_code == 403
        finally:
            await client.aclose()
            await engine.dispose()

    async def test_viewer_cannot_export_audit_log(self):
        client, engine, tenant_id = await _client_as("viewer")
        try:
            resp = await client.get(f"/api/tenants/{tenant_id}/audit/export")
            assert resp.status_code == 403
        finally:
            await client.aclose()
            await engine.dispose()

    async def test_viewer_cannot_read_billing(self):
        client, engine, tenant_id = await _client_as("viewer")
        try:
            resp = await client.get(f"/api/tenants/{tenant_id}/subscription")
            assert resp.status_code == 403
        finally:
            await client.aclose()
            await engine.dispose()

    async def test_viewer_cannot_list_invoices(self):
        client, engine, tenant_id = await _client_as("viewer")
        try:
            resp = await client.get(f"/api/tenants/{tenant_id}/invoices/")
            assert resp.status_code == 403
        finally:
            await client.aclose()
            await engine.dispose()

    async def test_viewer_cannot_download_a_license(self):
        client, engine, tenant_id = await _client_as("viewer")
        try:
            resp = await client.get(f"/api/tenants/{tenant_id}/licenses/")
            assert resp.status_code == 403
        finally:
            await client.aclose()
            await engine.dispose()


class TestPlatformElevation:
    """Crossing the platform/tenant boundary must be explicit and expiring.

    ``SupportAccessLog`` shipped in R2b with expiry, revocation and audit
    at enable/revoke — and gated nothing, because no authorization path
    consulted it. These pin that it now does.
    """

    async def test_platform_support_is_refused_without_a_grant(self):
        client, engine, tenant_id = await _client_as(
            "platform_support", platform=True,
        )
        try:
            resp = await client.get(f"/api/tenants/{tenant_id}/tickets/")
            assert resp.status_code == 403
            assert "support access" in resp.json()["detail"]
        finally:
            await client.aclose()
            await engine.dispose()

    async def test_grant_admits_then_revocation_refuses_again(self):
        client, engine, tenant_id = await _client_as(
            "platform_support", platform=True,
        )
        try:
            assert (
                await client.get(f"/api/tenants/{tenant_id}/tickets/")
            ).status_code == 403

            enabled = await client.post(
                f"/api/admin/support-access/{tenant_id}/enable",
            )
            assert enabled.status_code == 200
            assert enabled.json()["status"] == "enabled"

            assert (
                await client.get(f"/api/tenants/{tenant_id}/tickets/")
            ).status_code == 200

            revoked = await client.post(
                f"/api/admin/support-access/{tenant_id}/revoke",
            )
            assert revoked.status_code == 200

            assert (
                await client.get(f"/api/tenants/{tenant_id}/tickets/")
            ).status_code == 403
        finally:
            await client.aclose()
            await engine.dispose()

    async def test_grant_does_not_widen_beyond_the_role(self):
        """An active grant admits platform_support; it does not make it root.

        The grant governs *reaching* the tenant. Once inside, the caller is
        still only what its role says it is.
        """
        client, engine, tenant_id = await _client_as(
            "platform_support", platform=True,
        )
        try:
            await client.post(f"/api/admin/support-access/{tenant_id}/enable")
            # platform_support holds support.view and audit.view ...
            assert (
                await client.get(f"/api/tenants/{tenant_id}/tickets/")
            ).status_code == 200
            # ... and does not hold user.manage, so no minting API keys.
            resp = await client.post(
                f"/api/tenants/{tenant_id}/api-keys/",
                json={"name": "nope", "scope": "read"},
            )
            assert resp.status_code == 403, (
                f"platform_support minted a key: {resp.status_code} {resp.text}"
            )
        finally:
            await client.aclose()
            await engine.dispose()

    async def test_super_admin_keeps_break_glass(self):
        """Gating the break-glass on the grant mechanism would lock everyone
        out if that mechanism itself failed mid-incident."""
        client, engine, tenant_id = await _client_as(
            "platform_super_admin", platform=True,
        )
        try:
            assert (
                await client.get(f"/api/tenants/{tenant_id}/tickets/")
            ).status_code == 200
        finally:
            await client.aclose()
            await engine.dispose()


class TestEntitledRolesStillPass:
    """The fix must refuse the unentitled without breaking the entitled."""

    async def test_tenant_owner_may_list_api_keys(self):
        client, engine, tenant_id = await _client_as("tenant_owner")
        try:
            resp = await client.get(f"/api/tenants/{tenant_id}/api-keys/")
            assert resp.status_code == 200
        finally:
            await client.aclose()
            await engine.dispose()

    async def test_auditor_may_read_and_export_audit(self):
        client, engine, tenant_id = await _client_as("auditor")
        try:
            assert (
                await client.get(f"/api/tenants/{tenant_id}/audit/")
            ).status_code == 200
            assert (
                await client.get(f"/api/tenants/{tenant_id}/audit/export")
            ).status_code == 200
        finally:
            await client.aclose()
            await engine.dispose()

    async def test_auditor_may_read_billing_but_not_manage(self):
        """auditor holds billing.view, not billing.manage."""
        client, engine, tenant_id = await _client_as("auditor")
        try:
            # 404 (no subscription seeded) still proves the guard let it
            # through; 403 would mean billing.view was not honoured.
            assert (
                await client.get(f"/api/tenants/{tenant_id}/subscription")
            ).status_code != 403
            # usage/upload requires billing.manage. Sent as the real
            # multipart shape so the 403 is the guard, not a 422.
            resp = await client.post(
                f"/api/tenants/{tenant_id}/usage/upload",
                files={"file": ("usage.json", b"{}", "application/json")},
            )
            assert resp.status_code == 403
        finally:
            await client.aclose()
            await engine.dispose()

    async def test_operator_may_read_tickets_but_not_billing(self):
        client, engine, tenant_id = await _client_as("operator")
        try:
            assert (
                await client.get(f"/api/tenants/{tenant_id}/tickets/")
            ).status_code == 200
            assert (
                await client.get(f"/api/tenants/{tenant_id}/invoices/")
            ).status_code == 403
        finally:
            await client.aclose()
            await engine.dispose()
