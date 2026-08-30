"""E1.4: the platform/tenant realm boundary.

Realm membership answers "which tenant does this identity belong to?".
E1.2's scoped RBAC answers "what may it do, and over which scope?".
These are separate concerns and E1.4 does not merge them.
"""

from __future__ import annotations

import httpx
import pytest

from harkeniq_console.app import create_app
from harkeniq_console.config import ConsoleConfig
from harkeniq_console.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_console.db.repos import TenantRepo
from harkeniq_console.runtime import AppState


async def _stack():
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    sm = make_sessionmaker(engine)
    state = AppState(config=ConsoleConfig(insecure=True), engine=engine,
                     sessionmaker=sm)
    app = create_app(state)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    )
    return client, sm, state


class TestProvisioningIsRealAndRequired:
    @pytest.mark.asyncio
    async def test_creating_a_tenant_provisions_its_realm(self):
        client, sm, state = await _stack()
        async with client:
            resp = await client.post("/api/admin/tenants/", json={
                "name": "Acme", "slug": "acme", "admin_email": "owner@acme",
                "billing_country": "US", "currency": "USD",
            })
            assert resp.status_code in (200, 201), resp.text
            body = resp.json()
            # The defect: this used to be null, for every tenant, always.
            assert body["keycloak_realm"] == "acme"

        kc = state.keycloak_admin
        assert "acme" in kc._realms
        # The five tenant roles, in the TENANT realm.
        assert set(kc._realm_roles["acme"]) >= {
            "tenant_owner", "site_admin", "operator", "auditor", "viewer",
        }
        # And the console client, so tenant users have something to log
        # in through.
        assert kc._clients["acme"]

    @pytest.mark.asyncio
    async def test_the_binding_is_recorded_and_resolvable(self):
        client, sm, _ = await _stack()
        async with client:
            await client.post("/api/admin/tenants/", json={
                "name": "Acme", "slug": "acme",
                "billing_country": "US", "currency": "USD",
            })
        async with sm() as session:
            repo = TenantRepo(session)
            tenant = await repo.get_by_realm("acme")
            assert tenant is not None and tenant.slug == "acme"
            assert tenant.keycloak_realm == "acme"

    @pytest.mark.asyncio
    async def test_a_legacy_realmless_tenant_can_be_provisioned(self):
        """The path for tenants created before E1.4 had a realm at all."""
        client, sm, state = await _stack()
        async with sm() as session:
            tenant = await TenantRepo(session).create(
                name="Legacy", slug="legacy", billing_country="US",
                currency="USD",
            )
            tenant_id = tenant.id
            await session.commit()

        async with client:
            resp = await client.post(
                f"/api/admin/tenants/{tenant_id}/provision-realm"
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["keycloak_realm"] == "legacy"
            assert resp.json()["provisioned"] is True

            # Idempotent.
            again = await client.post(
                f"/api/admin/tenants/{tenant_id}/provision-realm"
            )
            assert again.json()["provisioned"] is False

    @pytest.mark.asyncio
    async def test_provisioning_an_unknown_tenant_is_404(self):
        client, _, _ = await _stack()
        async with client:
            resp = await client.post("/api/admin/tenants/nope/provision-realm")
            assert resp.status_code == 404


class TestRealmResolution:
    @pytest.mark.asyncio
    async def test_a_realm_nobody_recorded_resolves_to_nothing(self):
        """A stray realm on the same Keycloak cannot mint access."""
        client, sm, _ = await _stack()
        async with client:
            await client.post("/api/admin/tenants/", json={
                "name": "Acme", "slug": "acme",
                "billing_country": "US", "currency": "USD",
            })
        async with sm() as session:
            assert await TenantRepo(session).get_by_realm("stray") is None

    @pytest.mark.asyncio
    async def test_resolution_does_not_consult_the_slug(self):
        """The end state: slug is out of the identity path.

        A tenant whose realm differs from its slug must resolve by the
        realm and NOT by the slug, or the binding is decorative.
        """
        client, sm, _ = await _stack()
        async with sm() as session:
            tenant = await TenantRepo(session).create(
                name="Renamed", slug="old-slug", billing_country="US",
                currency="USD",
            )
            await TenantRepo(session).update(
                tenant, keycloak_realm="the-real-realm"
            )
            await session.commit()

        async with sm() as session:
            repo = TenantRepo(session)
            assert (await repo.get_by_realm("the-real-realm")).slug == "old-slug"
            # The slug is NOT an identity: a token from a realm named for
            # it resolves to nothing.
            assert await repo.get_by_realm("old-slug") is None

    def test_auth_resolves_by_realm_not_slug(self):
        import inspect

        from harkeniq_console import auth

        source = inspect.getsource(auth._resolve_tenant_id)
        assert "get_by_realm" in source
        assert "get_by_slug" not in source


class TestCentralCommandRealmBoundary:
    """Platform identity must not become a tenant operator."""

    def test_cc_refuses_to_boot_pinned_to_the_platform_realm(self):
        from harkeniq_cc.config import CCConfig

        errors = CCConfig(
            tenant_id="t", keycloak_realm="harkeniq-platform",
            platform_realm="harkeniq-platform",
        ).validate()
        assert errors, "CC accepted the platform realm as its tenant realm"
        assert "PLATFORM realm" in errors[0]

    def test_cc_accepts_a_tenant_realm(self):
        from harkeniq_cc.config import CCConfig

        assert CCConfig(
            tenant_id="t", keycloak_realm="tenant-demo",
            platform_realm="harkeniq-platform",
        ).validate() == []

    def test_a_lab_deployment_is_still_allowed(self):
        from harkeniq_cc.config import CCConfig

        assert CCConfig(
            tenant_id="t", insecure=True, keycloak_realm="harkeniq-platform",
        ).validate() == []

    def test_cc_still_pins_exactly_one_realm(self):
        """The mechanism that refuses another realm's token, unchanged."""
        import inspect

        from harkeniq_cc import auth

        source = inspect.getsource(auth.configure_auth)
        assert "realm_allowed=lambda r: r == realm" in source


class TestSeparationOfConcerns:
    """Realm answers WHICH TENANT. E1.2 answers WHAT and WHERE."""

    def test_the_realm_path_grants_no_permission_of_its_own(self):
        import inspect

        from harkeniq_console import auth

        source = inspect.getsource(auth._resolve_tenant_id)
        for forbidden in ("permission", "ROLE_PERMISSIONS", "scope", "grant"):
            assert forbidden not in source

    def test_provisioning_creates_no_new_role_vocabulary(self):
        from harkeniq_console.keycloak_admin import DEFAULT_REALM_ROLES
        from harkeniq_console.permissions import ROLE_PERMISSIONS

        # Exactly the five tenant roles that already exist, and nothing new.
        assert set(DEFAULT_REALM_ROLES) == {
            "tenant_owner", "site_admin", "operator", "auditor", "viewer",
        }
        assert set(DEFAULT_REALM_ROLES) <= set(ROLE_PERMISSIONS)

    def test_platform_roles_are_never_provisioned_into_a_tenant_realm(self):
        from harkeniq_console.keycloak_admin import DEFAULT_REALM_ROLES

        assert "platform_super_admin" not in DEFAULT_REALM_ROLES
        assert "platform_support" not in DEFAULT_REALM_ROLES


class TestCustomBundlesIntersect:
    """E1.4 objective 6: a bundle re-shapes authority WITHIN a role.

    It used to be OR-ed with the role, so a tenant could define a bundle
    naming any permission in the vocabulary -- including platform-side
    ones like `tenant.manage` -- and assign it to a viewer. That is the
    same widening E1.2 refused for `permission_subset`.
    """

    @pytest.mark.asyncio
    async def test_a_bundle_grants_only_what_the_role_also_holds(self):
        from harkeniq_console.db.repos import CustomRoleRepo, UserRepo
        from harkeniq_console.permissions import effective_permissions

        client, sm, _ = await _stack()
        async with client:
            resp = await client.post("/api/admin/tenants/", json={
                "name": "Acme", "slug": "acme",
                "billing_country": "US", "currency": "USD",
            })
            tenant_id = resp.json()["id"]

        async with sm() as session:
            user = await UserRepo(session).create(
                tenant_id=tenant_id, email="bundle@acme", role="viewer",
                keycloak_user_id="kc-bundle", status="active",
            )
            roles = CustomRoleRepo(session)
            bundle = await roles.create(
                tenant_id=tenant_id, name="Reader Plus",
                # fleet.view IS a viewer permission; site.manage and
                # tenant.manage are not.
                permissions=["fleet.view", "site.manage", "tenant.manage"],
                created_by="owner",
            )
            from harkeniq_console.db.models import UserCustomRole

            session.add(UserCustomRole(
                user_id=user.id, custom_role_id=bundle.id,
            ))
            await session.commit()

        async with sm() as session:
            granted = await effective_permissions(
                session, "kc-bundle", "bundle@acme", tenant_id, role="viewer"
            )
            assert granted == ["fleet.view"], granted
            assert "site.manage" not in granted
            assert "tenant.manage" not in granted, (
                "a bundle handed a viewer a PLATFORM permission"
            )

    @pytest.mark.asyncio
    async def test_the_same_bundle_gives_more_to_a_wider_role(self):
        """The bundle is a filter, not a grant: what it yields depends on
        the role it is applied to."""
        from harkeniq_console.db.repos import CustomRoleRepo, UserRepo
        from harkeniq_console.permissions import effective_permissions

        client, sm, _ = await _stack()
        async with client:
            resp = await client.post("/api/admin/tenants/", json={
                "name": "Acme", "slug": "acme",
                "billing_country": "US", "currency": "USD",
            })
            tenant_id = resp.json()["id"]

        async with sm() as session:
            user = await UserRepo(session).create(
                tenant_id=tenant_id, email="admin@acme", role="site_admin",
                keycloak_user_id="kc-admin", status="active",
            )
            roles = CustomRoleRepo(session)
            bundle = await roles.create(
                tenant_id=tenant_id, name="Reader Plus",
                permissions=["fleet.view", "site.manage", "tenant.manage"],
                created_by="owner",
            )
            from harkeniq_console.db.models import UserCustomRole

            session.add(UserCustomRole(
                user_id=user.id, custom_role_id=bundle.id,
            ))
            await session.commit()

        async with sm() as session:
            granted = await effective_permissions(
                session, "kc-admin", "admin@acme", tenant_id, role="site_admin"
            )
            # site_admin HOLDS site.manage, so the bundle yields it.
            assert set(granted) == {"fleet.view", "site.manage"}
            assert "tenant.manage" not in granted

    @pytest.mark.asyncio
    async def test_a_bundle_naming_nothing_the_role_holds_grants_nothing(self):
        from harkeniq_console.db.repos import CustomRoleRepo, UserRepo
        from harkeniq_console.permissions import effective_permissions

        client, sm, _ = await _stack()
        async with client:
            resp = await client.post("/api/admin/tenants/", json={
                "name": "Acme", "slug": "acme",
                "billing_country": "US", "currency": "USD",
            })
            tenant_id = resp.json()["id"]

        async with sm() as session:
            user = await UserRepo(session).create(
                tenant_id=tenant_id, email="v@acme", role="viewer",
                keycloak_user_id="kc-v", status="active",
            )
            roles = CustomRoleRepo(session)
            bundle = await roles.create(
                tenant_id=tenant_id, name="Escalate",
                permissions=["role.manage", "billing.manage"],
                created_by="owner",
            )
            from harkeniq_console.db.models import UserCustomRole

            session.add(UserCustomRole(
                user_id=user.id, custom_role_id=bundle.id,
            ))
            await session.commit()

        async with sm() as session:
            granted = await effective_permissions(
                session, "kc-v", "v@acme", tenant_id, role="viewer"
            )
            assert granted == []
