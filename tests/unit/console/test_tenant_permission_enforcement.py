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


def _support_user(tenant_id: str):
    """Switch the client back to platform_support after an approver acts."""
    async def _fake() -> UserContext:
        return UserContext(
            user_id="kc-sub-1", email="platform_support@example.com",
            tenant_id=None, role="platform_support", permissions=[],
            is_platform_user=True,
        )
    return _fake


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


class TestTenantExistence:
    async def test_unknown_tenant_404s_rather_than_looking_empty(self):
        """A route that filters on a bogus id returns 200 and an empty list,
        which reads as "no data" instead of "no such tenant"."""
        client, engine, _tenant_id = await _client_as("platform_super_admin",
                                                      platform=True)
        try:
            resp = await client.get("/api/tenants/no-such-tenant/audit/")
            assert resp.status_code == 404
        finally:
            await client.aclose()
            await engine.dispose()

    async def test_a_tenant_user_cannot_probe_which_tenants_exist(self):
        """403 before 404: otherwise the status code is an existence oracle.

        A tenant user naming a real other tenant and a made-up one must get
        the same answer.
        """
        client, engine, tenant_id = await _client_as("tenant_owner")
        try:
            app = client._transport.app  # type: ignore[attr-defined]
            # Seed a second, real tenant this caller does not belong to.
            other = "b" * 32
            async with app.state.console.sessionmaker() as session:
                session.add(Tenant(id=other, name="Other", slug="other",
                                   billing_country="US"))
                await session.commit()

            real_other = await client.get(f"/api/tenants/{other}/audit/")
            made_up = await client.get("/api/tenants/no-such-tenant/audit/")
            assert real_other.status_code == made_up.status_code == 403
            assert real_other.json() == made_up.json()
            assert tenant_id != other
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

    async def test_request_alone_grants_nothing(self):
        """Asking is not being granted. The whole approval flow rests here."""
        client, engine, tenant_id = await _client_as(
            "platform_support", platform=True,
        )
        try:
            requested = await client.post(
                f"/api/admin/support-access/{tenant_id}/request",
                json={"reason": "ticket 123"},
            )
            assert requested.status_code == 200
            assert requested.json()["status"] == "requested"

            resp = await client.get(f"/api/tenants/{tenant_id}/tickets/")
            assert resp.status_code == 403, (
                f"a pending request admitted support: {resp.status_code}"
            )
        finally:
            await client.aclose()
            await engine.dispose()

    async def test_support_cannot_approve_its_own_request(self):
        client, engine, tenant_id = await _client_as(
            "platform_support", platform=True,
        )
        try:
            requested = await client.post(
                f"/api/admin/support-access/{tenant_id}/request",
                json={"reason": "ticket 123"},
            )
            rid = requested.json()["access"]["id"]

            resp = await client.post(
                f"/api/admin/support-access/requests/{rid}/approve",
            )
            assert resp.status_code == 403
            assert (
                await client.get("/api/admin/support-access/requests/pending")
            ).status_code == 403
        finally:
            await client.aclose()
            await engine.dispose()

    async def test_approval_admits_then_revocation_refuses_again(self):
        """The full chain: request → approve → admitted → revoke → refused."""
        client, engine, tenant_id = await _client_as(
            "platform_support", platform=True,
        )
        try:
            assert (
                await client.get(f"/api/tenants/{tenant_id}/tickets/")
            ).status_code == 403

            requested = await client.post(
                f"/api/admin/support-access/{tenant_id}/request",
                json={"reason": "ticket 123"},
            )
            rid = requested.json()["access"]["id"]

            # Approver is a different person with a different role.
            app = client._transport.app  # type: ignore[attr-defined]

            async def _admin() -> UserContext:
                return UserContext(
                    user_id="kc-admin", email="admin@example.com",
                    tenant_id=None, role="platform_super_admin",
                    permissions=[], is_platform_user=True,
                )

            app.dependency_overrides[get_current_user] = _admin
            pending = await client.get("/api/admin/support-access/requests/pending")
            assert pending.status_code == 200
            assert [r["id"] for r in pending.json()["items"]] == [rid]

            approved = await client.post(
                f"/api/admin/support-access/requests/{rid}/approve",
            )
            assert approved.status_code == 200
            assert approved.json()["access"]["approved_by"] == "kc-admin"

            app.dependency_overrides[get_current_user] = _support_user(tenant_id)
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

    async def test_denied_request_admits_nobody(self):
        client, engine, tenant_id = await _client_as(
            "platform_support", platform=True,
        )
        try:
            requested = await client.post(
                f"/api/admin/support-access/{tenant_id}/request",
                json={"reason": "nope"},
            )
            rid = requested.json()["access"]["id"]

            app = client._transport.app  # type: ignore[attr-defined]

            async def _admin() -> UserContext:
                return UserContext(
                    user_id="kc-admin", email="admin@example.com",
                    tenant_id=None, role="platform_super_admin",
                    permissions=[], is_platform_user=True,
                )

            app.dependency_overrides[get_current_user] = _admin
            denied = await client.post(
                f"/api/admin/support-access/requests/{rid}/deny",
            )
            assert denied.status_code == 200

            app.dependency_overrides[get_current_user] = _support_user(tenant_id)
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
            requested = await client.post(
                f"/api/admin/support-access/{tenant_id}/request",
                json={"reason": "ticket 123"},
            )
            rid = requested.json()["access"]["id"]
            app = client._transport.app  # type: ignore[attr-defined]

            async def _admin() -> UserContext:
                return UserContext(
                    user_id="kc-admin", email="admin@example.com",
                    tenant_id=None, role="platform_super_admin",
                    permissions=[], is_platform_user=True,
                )

            app.dependency_overrides[get_current_user] = _admin
            await client.post(
                f"/api/admin/support-access/requests/{rid}/approve",
            )
            app.dependency_overrides[get_current_user] = _support_user(tenant_id)

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


class TestGrantsBindToTheirRequester:
    """Red-team finding: a tenant-scoped grant admitted EVERY support
    engineer once ONE was approved. A grant is per person."""

    async def test_engineer_b_is_not_admitted_by_engineer_a_grant(self):
        client, engine, tenant_id = await _client_as(
            "platform_support", platform=True,
        )
        try:
            app = client._transport.app  # type: ignore[attr-defined]

            # Engineer A requests; the super admin approves it.
            requested = await client.post(
                f"/api/admin/support-access/{tenant_id}/request",
                json={"reason": "ticket 1"},
            )
            rid = requested.json()["access"]["id"]

            async def _admin() -> UserContext:
                return UserContext(
                    user_id="kc-admin", email="admin@example.com",
                    tenant_id=None, role="platform_super_admin",
                    permissions=[], is_platform_user=True,
                )

            app.dependency_overrides[get_current_user] = _admin
            assert (
                await client.post(
                    f"/api/admin/support-access/requests/{rid}/approve",
                )
            ).status_code == 200

            # Engineer A (kc-sub-1) is admitted.
            app.dependency_overrides[get_current_user] = _support_user(tenant_id)
            assert (
                await client.get(f"/api/tenants/{tenant_id}/tickets/")
            ).status_code == 200

            # Engineer B is NOT — the grant is A's, not all of support's.
            async def _engineer_b() -> UserContext:
                return UserContext(
                    user_id="kc-sub-2", email="b@example.com",
                    tenant_id=None, role="platform_support",
                    permissions=[], is_platform_user=True,
                )

            app.dependency_overrides[get_current_user] = _engineer_b
            resp = await client.get(f"/api/tenants/{tenant_id}/tickets/")
            assert resp.status_code == 403, (
                "engineer B rode engineer A's grant"
            )
            # And B's own status view shows no grant and no pending request.
            status = (
                await client.get(f"/api/admin/support-access/{tenant_id}")
            ).json()
            assert status == {"active": False, "pending": False}
        finally:
            await client.aclose()
            await engine.dispose()

    async def test_status_endpoint_reports_all_three_shapes(self):
        """no rows -> {active:False,pending:False}; after request ->
        pending; after approval -> active. The UI's enter flow branches on
        exactly these (previously untested — testing pass)."""
        client, engine, tenant_id = await _client_as(
            "platform_support", platform=True,
        )
        try:
            app = client._transport.app  # type: ignore[attr-defined]
            base = f"/api/admin/support-access/{tenant_id}"

            first = (await client.get(base)).json()
            assert first == {"active": False, "pending": False}

            rid = (
                await client.post(f"{base}/request", json={"reason": "t"})
            ).json()["access"]["id"]
            second = (await client.get(base)).json()
            assert second["active"] is False and second["pending"] is True
            assert second["access"]["id"] == rid

            async def _admin() -> UserContext:
                return UserContext(
                    user_id="kc-admin", email="admin@example.com",
                    tenant_id=None, role="platform_super_admin",
                    permissions=[], is_platform_user=True,
                )

            app.dependency_overrides[get_current_user] = _admin
            await client.post(f"/api/admin/support-access/requests/{rid}/approve")
            app.dependency_overrides[get_current_user] = _support_user(tenant_id)

            third = (await client.get(base)).json()
            assert third["active"] is True
            assert third["access"]["approved_by"] == "kc-admin"
        finally:
            await client.aclose()
            await engine.dispose()

    async def test_two_engineers_may_request_independently(self):
        """Per-requester dedupe: A's pending request must not block B's."""
        client, engine, tenant_id = await _client_as(
            "platform_support", platform=True,
        )
        try:
            app = client._transport.app  # type: ignore[attr-defined]
            base = f"/api/admin/support-access/{tenant_id}"
            assert (await client.post(f"{base}/request", json={"reason": "a"})
                    ).json()["status"] == "requested"
            # Same engineer again -> deduped.
            assert (await client.post(f"{base}/request", json={"reason": "a"})
                    ).json()["status"] == "already_requested"

            async def _engineer_b() -> UserContext:
                return UserContext(
                    user_id="kc-sub-2", email="b@example.com",
                    tenant_id=None, role="platform_support",
                    permissions=[], is_platform_user=True,
                )

            app.dependency_overrides[get_current_user] = _engineer_b
            assert (await client.post(f"{base}/request", json={"reason": "b"})
                    ).json()["status"] == "requested"
        finally:
            await client.aclose()
            await engine.dispose()


class TestRegistryIsPlatformPlane:
    """[review CRITICAL, 4 passes] tenant.view is shared vocabulary —
    tenant_owner holds it and it sits inside the custom-role ceiling — so
    a bare permission guard leaked the vendor registry to customers."""

    async def test_tenant_owner_cannot_read_the_platform_registry(self):
        client, engine, tenant_id = await _client_as("tenant_owner")
        try:
            assert (
                await client.get("/api/admin/tenants/")
            ).status_code == 403
            assert (
                await client.get(f"/api/admin/tenants/{tenant_id}")
            ).status_code == 403
        finally:
            await client.aclose()
            await engine.dispose()

    async def test_platform_support_may_list_but_not_manage(self):
        """The change's stated purpose (spec role 2 reads the registry),
        finally pinned server-side in both directions."""
        client, engine, tenant_id = await _client_as(
            "platform_support", platform=True,
        )
        try:
            assert (
                await client.get("/api/admin/tenants/")
            ).status_code == 200
            assert (
                await client.get(f"/api/admin/tenants/{tenant_id}")
            ).status_code == 200
            assert (
                await client.post(
                    f"/api/admin/tenants/{tenant_id}/suspend",
                    json={"reason": "x"},
                )
            ).status_code == 403
        finally:
            await client.aclose()
            await engine.dispose()


class TestTenantScopedInstall:
    """[review CRITICAL, api-contract] platform users' installs recorded
    nothing — 200 returned, CC never delivered. The tenant-plane page now
    names the tenant; decided by Vinod 2026-08-28."""

    async def _publish_skill(self, client) -> str:
        from harkeniq_console.db.models import MarketplaceSkill

        app = client._transport.app  # type: ignore[attr-defined]
        async with app.state.console.sessionmaker() as session:
            skill = MarketplaceSkill(
                skill_name="fan-check", version=1,
                author_email="t@example.com",
                yaml_content="name: fan-check",
                review_status="approved", published=True,
            )
            session.add(skill)
            await session.flush()
            sid = skill.id
            await session.commit()
        return sid

    async def test_platform_install_names_the_tenant_and_records(self):
        client, engine, tenant_id = await _client_as(
            "platform_super_admin", platform=True,
        )
        try:
            sid = await self._publish_skill(client)
            resp = await client.post(
                f"/api/marketplace/skills/{sid}/install",
                json={"tenant_id": tenant_id},
            )
            assert resp.status_code == 200

            from harkeniq_console.db.models import MarketplaceInstall
            from sqlalchemy import select

            app = client._transport.app  # type: ignore[attr-defined]
            async with app.state.console.sessionmaker() as session:
                rows = (
                    await session.execute(select(MarketplaceInstall))
                ).scalars().all()
            assert [r.tenant_id for r in rows] == [tenant_id], (
                "install was not recorded for the named tenant"
            )
        finally:
            await client.aclose()
            await engine.dispose()

    async def test_tenant_user_cannot_install_for_another_tenant(self):
        client, engine, tenant_id = await _client_as("tenant_owner")
        try:
            sid = await self._publish_skill(client)
            other = "c" * 32
            app = client._transport.app  # type: ignore[attr-defined]
            async with app.state.console.sessionmaker() as session:
                session.add(Tenant(id=other, name="Other", slug="other-t",
                                   billing_country="US"))
                await session.commit()
            resp = await client.post(
                f"/api/marketplace/skills/{sid}/install",
                json={"tenant_id": other},
            )
            assert resp.status_code == 403
        finally:
            await client.aclose()
            await engine.dispose()

    async def test_tenant_user_default_stays_their_own(self):
        client, engine, tenant_id = await _client_as("tenant_owner")
        try:
            sid = await self._publish_skill(client)
            assert (
                await client.post(f"/api/marketplace/skills/{sid}/install")
            ).status_code == 200

            from harkeniq_console.db.models import MarketplaceInstall
            from sqlalchemy import select

            app = client._transport.app  # type: ignore[attr-defined]
            async with app.state.console.sessionmaker() as session:
                rows = (
                    await session.execute(select(MarketplaceInstall))
                ).scalars().all()
            assert [r.tenant_id for r in rows] == [tenant_id]
        finally:
            await client.aclose()
            await engine.dispose()


class TestA14DenialSemantics:
    """A14 (OQ-25, decided by Vinod): denial is non-final — re-request is
    allowed — but the approver sees the engineer's denial history for that
    tenant at decision time. No cooldowns, no locks."""

    async def _request_then_deny(self, client, tenant_id, reason="not now"):
        app = client._transport.app  # type: ignore[attr-defined]
        rid = (
            await client.post(
                f"/api/admin/support-access/{tenant_id}/request",
                json={"reason": reason},
            )
        ).json()["access"]["id"]

        async def _admin() -> UserContext:
            return UserContext(
                user_id="kc-admin", email="admin@example.com",
                tenant_id=None, role="platform_super_admin",
                permissions=[], is_platform_user=True,
            )

        app.dependency_overrides[get_current_user] = _admin
        assert (
            await client.post(
                f"/api/admin/support-access/requests/{rid}/deny",
            )
        ).status_code == 200
        app.dependency_overrides[get_current_user] = _support_user(tenant_id)
        return rid

    async def test_rerequest_after_denial_is_allowed(self):
        """Half one of A14: a deny does not permanently deny the person."""
        client, engine, tenant_id = await _client_as(
            "platform_support", platform=True,
        )
        try:
            await self._request_then_deny(client, tenant_id)
            again = await client.post(
                f"/api/admin/support-access/{tenant_id}/request",
                json={"reason": "new ticket, new window"},
            )
            assert again.json()["status"] == "requested"
        finally:
            await client.aclose()
            await engine.dispose()

    async def test_approver_sees_denial_history_at_decision_time(self):
        """Half two: the history is never hidden from the next decision."""
        client, engine, tenant_id = await _client_as(
            "platform_support", platform=True,
        )
        try:
            app = client._transport.app  # type: ignore[attr-defined]
            await self._request_then_deny(client, tenant_id, reason="ask one")
            await self._request_then_deny(client, tenant_id, reason="ask two")
            await client.post(
                f"/api/admin/support-access/{tenant_id}/request",
                json={"reason": "third ask"},
            )

            async def _admin() -> UserContext:
                return UserContext(
                    user_id="kc-admin", email="admin@example.com",
                    tenant_id=None, role="platform_super_admin",
                    permissions=[], is_platform_user=True,
                )

            app.dependency_overrides[get_current_user] = _admin
            queue = (
                await client.get("/api/admin/support-access/requests/pending")
            ).json()["items"]
            assert len(queue) == 1
            history = queue[0]["prior_denials"]
            assert history["count"] == 2
            assert history["last_reason"] == "ask two"
            assert history["last_denied_at"] is not None
        finally:
            await client.aclose()
            await engine.dispose()


class TestA13AuditorScope:
    """A13 (OQ-24, decided by Vinod): the auditor is read-only everything —
    every atomic *.view plus audit.export, and nothing else. Pinned as an
    EXACT set so any future widening or narrowing is a deliberate diff."""

    def test_auditor_set_is_exactly_every_view_plus_export(self):
        from harkeniq_console.permissions import PERMISSIONS, ROLE_PERMISSIONS

        expected = {p for p in PERMISSIONS if p.endswith(".view")}
        expected -= {"tenant.view"}  # platform-plane read since A11.4
        expected |= {"audit.export"}
        assert ROLE_PERMISSIONS["auditor"] == expected

    async def test_auditor_gains_the_four_a13_reads(self):
        client, engine, tenant_id = await _client_as("auditor")
        try:
            # support.view — ticket-trail evidence (UC6)
            assert (
                await client.get(f"/api/tenants/{tenant_id}/tickets/")
            ).status_code == 200
            # license.view — entitlement compliance (UC5)
            assert (
                await client.get(f"/api/tenants/{tenant_id}/licenses/")
            ).status_code == 200
            # user.view — access review (UC1)
            assert (
                await client.get(f"/api/tenants/{tenant_id}/users/")
            ).status_code == 200
        finally:
            await client.aclose()
            await engine.dispose()

    async def test_auditor_still_cannot_mutate_anything(self):
        """The A13 hard boundary: no write, admin, elevation, or action
        capability of any kind."""
        client, engine, tenant_id = await _client_as("auditor")
        try:
            # no API-key minting (user.manage)
            assert (
                await client.post(
                    f"/api/tenants/{tenant_id}/api-keys/",
                    json={"name": "nope", "scope": "read"},
                )
            ).status_code == 403
            # no ticket creation (support.create)
            assert (
                await client.post(
                    f"/api/tenants/{tenant_id}/tickets/",
                    json={"subject": "s", "severity": "S3",
                          "component": "Other", "body": "b"},
                )
            ).status_code == 403
            # no usage upload (billing.manage)
            assert (
                await client.post(
                    f"/api/tenants/{tenant_id}/usage/upload",
                    files={"file": ("u.json", b"{}", "application/json")},
                )
            ).status_code == 403
        finally:
            await client.aclose()
            await engine.dispose()

    async def test_auditor_has_no_tenant_discovery(self):
        """No cross-tenant access through the auditor role: the platform
        registry refuses it, and another tenant's data refuses it."""
        client, engine, tenant_id = await _client_as("auditor")
        try:
            assert (
                await client.get("/api/admin/tenants/")
            ).status_code == 403
            app = client._transport.app  # type: ignore[attr-defined]
            other = "d" * 32
            async with app.state.console.sessionmaker() as session:
                session.add(Tenant(id=other, name="Other2", slug="other-2",
                                   billing_country="US"))
                await session.commit()
            assert (
                await client.get(f"/api/tenants/{other}/tickets/")
            ).status_code == 403
        finally:
            await client.aclose()
            await engine.dispose()
