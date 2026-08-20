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
    async def test_create_tenant_basic(self, svc_no_kc, session):
        result = await svc_no_kc.create_tenant(_req())
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

    async def test_create_tenant_with_owner(self, svc_no_kc, session):
        result = await svc_no_kc.create_tenant(
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

    async def test_create_tenant_slug_conflict(self, svc_no_kc, session):
        await svc_no_kc.create_tenant(_req())
        await session.commit()
        with pytest.raises(TenantError, match="already exists"):
            await svc_no_kc.create_tenant(_req())


class TestSuspendTenant:
    async def test_suspend_tenant(self, svc_no_kc, session):
        result = await svc_no_kc.create_tenant(_req())
        await session.commit()
        sus = await svc_no_kc.suspend_tenant(
            result["id"], "billing lapse", "admin@harkeniq.com",
        )
        await session.commit()
        assert sus["status"] == "suspended"
        tenant = await TenantRepo(session).get_by_id(result["id"])
        assert tenant.status == "suspended"
        assert tenant.suspended_reason == "billing lapse"

    async def test_suspend_already_suspended(self, svc_no_kc, session):
        result = await svc_no_kc.create_tenant(_req())
        await session.commit()
        await svc_no_kc.suspend_tenant(
            result["id"], "billing", "admin@harkeniq.com",
        )
        await session.commit()
        with pytest.raises(TenantError, match="already suspended"):
            await svc_no_kc.suspend_tenant(
                result["id"], "again", "admin@harkeniq.com",
            )

    async def test_suspend_not_found(self, svc_no_kc):
        with pytest.raises(TenantError, match="not found"):
            await svc_no_kc.suspend_tenant(
                "nonexistent", "reason", "admin@harkeniq.com",
            )


class TestReactivateTenant:
    async def test_reactivate_tenant(self, svc_no_kc, session):
        result = await svc_no_kc.create_tenant(_req())
        await session.commit()
        await svc_no_kc.suspend_tenant(
            result["id"], "billing", "admin@harkeniq.com",
        )
        await session.commit()
        reac = await svc_no_kc.reactivate_tenant(
            result["id"], "admin@harkeniq.com",
        )
        await session.commit()
        assert reac["status"] == "active"
        tenant = await TenantRepo(session).get_by_id(result["id"])
        assert tenant.status == "active"
        assert tenant.suspended_at is None
        assert tenant.suspended_reason is None

    async def test_reactivate_not_suspended(self, svc_no_kc, session):
        result = await svc_no_kc.create_tenant(_req())
        await session.commit()
        with pytest.raises(TenantError, match="not suspended"):
            await svc_no_kc.reactivate_tenant(
                result["id"], "admin@harkeniq.com",
            )


class TestGetTenantDetail:
    async def test_get_tenant_detail(self, svc_no_kc, session):
        result = await svc_no_kc.create_tenant(
            _req(admin_email="owner@acme.com"),
        )
        await session.commit()
        detail = await svc_no_kc.get_tenant_detail(result["id"])
        assert detail is not None
        assert detail["slug"] == "acme"
        assert detail["user_count"] == 1
        assert "created_at" in detail
        assert "billing_country" in detail

    async def test_get_tenant_not_found(self, svc_no_kc):
        detail = await svc_no_kc.get_tenant_detail("nonexistent")
        assert detail is None


class TestAuditEntries:
    async def test_create_audit_entries(self, svc_no_kc, session):
        result = await svc_no_kc.create_tenant(_req())
        await session.commit()
        await svc_no_kc.suspend_tenant(
            result["id"], "billing", "admin@harkeniq.com",
        )
        await session.commit()
        await svc_no_kc.reactivate_tenant(
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
