"""TenantService lifecycle tests (in-memory DB, no real Keycloak)."""

import pytest

from harkeniq_console.db.repos import AuditRepo, TenantRepo, UserRepo
from harkeniq_console.keycloak_admin import MockKeycloakAdminClient
from harkeniq_console.tenant_service import (
    TenantCreateRequest,
    TenantError,
    TenantService,
)


@pytest.fixture
def keycloak():
    return MockKeycloakAdminClient()


@pytest.fixture
def svc(session, keycloak):
    return TenantService(session, keycloak_admin=keycloak)


@pytest.fixture
def svc_no_kc(session):
    """E1.4: a service that CANNOT provision an identity boundary.

    Kept for exactly one thing -- proving that creating a tenant through
    it is refused. Before E1.4 this fixture created tenants happily, with
    keycloak_realm null, which is the production behaviour this slice
    removes.
    """
    return TenantService(session, keycloak_admin=None)


def _req(**kwargs) -> TenantCreateRequest:
    defaults = dict(
        name="Acme Corp",
        slug="acme",
        billing_country="US",
        currency="USD",
        plan="approve",
        node_commit=100,
        admin_email="",
    )
    defaults.update(kwargs)
    return TenantCreateRequest(**defaults)


class TestCreateTenant:
    async def test_create_tenant_basic(self, svc, session):
        result = await svc.create_tenant(_req())
        await session.commit()
        assert result["slug"] == "acme"
        assert result["status"] == "active"
        assert result["id"]
        # Audit entry created
        audit_items, total = await AuditRepo(session).list_filtered(
            tenant_id=result["id"],
        )
        assert total >= 1
        actions = [a.action for a in audit_items]
        assert "tenant.create" in actions

    async def test_create_tenant_with_owner(self, svc, session):
        result = await svc.create_tenant(
            _req(admin_email="owner@acme.com"),
        )
        await session.commit()
        assert result["owner_email"] == "owner@acme.com"
        assert result["owner_id"] is not None
        # User row exists
        user = await UserRepo(session).get_by_email(result["id"], "owner@acme.com")
        assert user is not None
        assert user.role == "tenant_owner"

    async def test_create_tenant_with_keycloak(self, svc, keycloak, session):
        result = await svc.create_tenant(
            _req(admin_email="admin@acme.com"),
        )
        await session.commit()
        assert result["keycloak_realm"] == "acme"
        # Keycloak realm was created
        assert "acme" in keycloak._realms

    async def test_create_tenant_slug_conflict(self, svc, session):
        await svc.create_tenant(_req())
        await session.commit()
        with pytest.raises(TenantError, match="already exists"):
            await svc.create_tenant(_req())


class TestSuspendTenant:
    async def test_suspend_tenant(self, svc, session):
        result = await svc.create_tenant(_req())
        await session.commit()
        sus = await svc.suspend_tenant(
            result["id"], "billing lapse", "admin@harkeniq.com",
        )
        await session.commit()
        assert sus["status"] == "suspended"
        tenant = await TenantRepo(session).get_by_id(result["id"])
        assert tenant.status == "suspended"
        assert tenant.suspended_reason == "billing lapse"

    async def test_suspend_already_suspended(self, svc, session):
        result = await svc.create_tenant(_req())
        await session.commit()
        await svc.suspend_tenant(
            result["id"], "billing", "admin@harkeniq.com",
        )
        await session.commit()
        with pytest.raises(TenantError, match="already suspended"):
            await svc.suspend_tenant(
                result["id"], "again", "admin@harkeniq.com",
            )

    async def test_suspend_not_found(self, svc):
        with pytest.raises(TenantError, match="not found"):
            await svc.suspend_tenant(
                "nonexistent", "reason", "admin@harkeniq.com",
            )


class TestReactivateTenant:
    async def test_reactivate_tenant(self, svc, session):
        result = await svc.create_tenant(_req())
        await session.commit()
        await svc.suspend_tenant(
            result["id"], "billing", "admin@harkeniq.com",
        )
        await session.commit()
        reac = await svc.reactivate_tenant(
            result["id"], "admin@harkeniq.com",
        )
        await session.commit()
        assert reac["status"] == "active"
        tenant = await TenantRepo(session).get_by_id(result["id"])
        assert tenant.status == "active"
        assert tenant.suspended_at is None
        assert tenant.suspended_reason is None

    async def test_reactivate_not_suspended(self, svc, session):
        result = await svc.create_tenant(_req())
        await session.commit()
        with pytest.raises(TenantError, match="not suspended"):
            await svc.reactivate_tenant(
                result["id"], "admin@harkeniq.com",
            )


class TestGetTenantDetail:
    async def test_get_tenant_detail(self, svc, session):
        result = await svc.create_tenant(
            _req(admin_email="owner@acme.com"),
        )
        await session.commit()
        detail = await svc.get_tenant_detail(result["id"])
        assert detail is not None
        assert detail["slug"] == "acme"
        assert detail["user_count"] == 1
        assert "created_at" in detail
        assert "billing_country" in detail

    async def test_get_tenant_not_found(self, svc):
        detail = await svc.get_tenant_detail("nonexistent")
        assert detail is None


class TestAuditEntries:
    async def test_create_audit_entries(self, svc, session):
        result = await svc.create_tenant(_req())
        await session.commit()
        await svc.suspend_tenant(
            result["id"], "billing", "admin@harkeniq.com",
        )
        await session.commit()
        await svc.reactivate_tenant(
            result["id"], "admin@harkeniq.com",
        )
        await session.commit()
        items, total = await AuditRepo(session).list_filtered(
            tenant_id=result["id"],
        )
        actions = sorted(a.action for a in items)
        assert "tenant.create" in actions
        assert "tenant.reactivate" in actions
        assert "tenant.suspend" in actions


class TestIdentityBoundaryIsRequired:
    """E1.4: a tenant without its realm is not a tenant.

    `TenantService(session)` was constructed with no Keycloak client at
    all four production call sites, so `if self.keycloak:` was always
    false: creating a tenant returned 200 with keycloak_realm null, no
    realm, no roles, no client and no owner, and nothing said so.
    """

    @pytest.mark.asyncio
    async def test_creating_without_a_provisioner_is_refused(
        self, svc_no_kc, session
    ):
        with pytest.raises(TenantError) as exc:
            await svc_no_kc.create_tenant(_req())
        assert exc.value.code == "keycloak_unconfigured"

    @pytest.mark.asyncio
    async def test_nothing_is_written_when_provisioning_is_impossible(
        self, svc_no_kc, session
    ):
        with pytest.raises(TenantError):
            await svc_no_kc.create_tenant(_req())
        assert await TenantRepo(session).get_by_slug("acme") is None

    @pytest.mark.asyncio
    async def test_a_created_tenant_always_has_its_realm_recorded(
        self, svc, session
    ):
        result = await svc.create_tenant(_req())
        assert result["keycloak_realm"] == "acme"
        tenant = await TenantRepo(session).get_by_slug("acme")
        assert tenant.keycloak_realm == "acme"

    @pytest.mark.asyncio
    async def test_the_realm_is_resolvable_as_the_authoritative_binding(
        self, svc, session
    ):
        await svc.create_tenant(_req())
        tenant = await TenantRepo(session).get_by_realm("acme")
        assert tenant is not None and tenant.slug == "acme"
        assert await TenantRepo(session).get_by_realm("nobody") is None

    @pytest.mark.asyncio
    async def test_a_failing_keycloak_rolls_the_tenant_back(
        self, session, keycloak
    ):
        """Fail closed: no half-provisioned tenant reported as created."""

        async def _boom(slug):
            raise RuntimeError("keycloak unreachable")

        keycloak.create_realm = _boom
        svc = TenantService(session, keycloak_admin=keycloak)
        with pytest.raises(TenantError) as exc:
            await svc.create_tenant(_req())
        assert exc.value.code == "keycloak_error"
        assert await TenantRepo(session).get_by_slug("acme") is None

    @pytest.mark.asyncio
    async def test_provisioning_an_existing_tenant_is_idempotent(
        self, svc, session
    ):
        """The path for tenants that predate E1.4 and have no realm."""
        await svc.create_tenant(_req())
        tenant = await TenantRepo(session).get_by_slug("acme")
        again = await svc.provision_realm(tenant)
        assert again == "acme"

    @pytest.mark.asyncio
    async def test_a_realmless_legacy_tenant_can_be_provisioned(
        self, svc, session
    ):
        tenant = await TenantRepo(session).create(
            name="Legacy", slug="legacy", billing_country="US", currency="USD",
        )
        assert tenant.keycloak_realm is None
        realm = await svc.provision_realm(tenant, actor="platform-admin")
        assert realm == "legacy"
        assert tenant.keycloak_realm == "legacy"


class TestProductionWiring:
    """The defect was the wiring, so the wiring is what a test must pin."""

    def test_every_tenant_service_call_site_injects_a_client(self):
        import inspect

        from harkeniq_console.api import tenants as tenants_api

        source = inspect.getsource(tenants_api)
        bare = source.count("TenantService(session)")
        assert bare == 0, (
            "a production TenantService is built with no keycloak_admin: "
            "that is the construction that made every tenant realm-less"
        )
        assert "TenantService(session, keycloak_admin=" in source

    def test_the_real_client_is_constructed_in_runtime(self):
        import inspect

        from harkeniq_console import runtime

        source = inspect.getsource(runtime)
        assert "KeycloakAdminClient(" in source, (
            "the real admin client is still never instantiated outside "
            "its own module"
        )
