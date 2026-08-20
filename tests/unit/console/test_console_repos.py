"""Console repository behavior against in-memory aiosqlite."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from harkeniq_console.db.repos import (
    AuditRepo,
    CustomRoleRepo,
    SettingsRepo,
    TenantRepo,
    UserRepo,
)


def now():
    return datetime.now(timezone.utc)


@pytest.fixture
async def tenant(session):
    repo = TenantRepo(session)
    return await repo.create(slug="acme", name="Acme Corp")


@pytest.fixture
async def user(session, tenant):
    repo = UserRepo(session)
    return await repo.create(
        tenant_id=tenant.id, email="alice@acme.com",
        display_name="Alice", role="operator",
    )


class TestTenantRepo:
    async def test_create(self, session):
        repo = TenantRepo(session)
        t = await repo.create(slug="acme", name="Acme Corp")
        assert t.slug == "acme"
        assert t.status == "active"

    async def test_get_by_id(self, session, tenant):
        repo = TenantRepo(session)
        found = await repo.get_by_id(tenant.id)
        assert found is not None
        assert found.slug == "acme"

    async def test_get_by_slug(self, session, tenant):
        repo = TenantRepo(session)
        found = await repo.get_by_slug("acme")
        assert found is not None
        assert found.id == tenant.id

    async def test_list_filtered_empty(self, session):
        repo = TenantRepo(session)
        items, total = await repo.list_filtered()
        assert items == []
        assert total == 0

    async def test_list_filtered_search(self, session, tenant):
        repo = TenantRepo(session)
        items, total = await repo.list_filtered(search="Acme")
        assert total == 1
        assert items[0].slug == "acme"

    async def test_list_filtered_status(self, session, tenant):
        repo = TenantRepo(session)
        items, total = await repo.list_filtered(status="active")
        assert total == 1
        items_sus, total_sus = await repo.list_filtered(status="suspended")
        assert total_sus == 0

    async def test_count(self, session, tenant):
        repo = TenantRepo(session)
        assert await repo.count() == 1

    async def test_update(self, session, tenant):
        repo = TenantRepo(session)
        updated = await repo.update(tenant, name="Acme Industries")
        assert updated.name == "Acme Industries"
        assert updated.updated_at is not None


class TestUserRepo:
    async def test_create(self, session, tenant):
        repo = UserRepo(session)
        u = await repo.create(
            tenant_id=tenant.id, email="bob@acme.com", role="viewer",
        )
        assert u.email == "bob@acme.com"
        assert u.status == "invited"

    async def test_get_by_id(self, session, user):
        repo = UserRepo(session)
        found = await repo.get_by_id(user.id)
        assert found is not None
        assert found.email == "alice@acme.com"

    async def test_get_by_email(self, session, tenant, user):
        repo = UserRepo(session)
        found = await repo.get_by_email(tenant.id, "alice@acme.com")
        assert found is not None
        assert found.id == user.id

    async def test_list_by_tenant(self, session, tenant, user):
        repo = UserRepo(session)
        items, total = await repo.list_by_tenant(tenant.id)
        assert total == 1
        assert items[0].email == "alice@acme.com"

    async def test_list_filtered_role(self, session, tenant, user):
        repo = UserRepo(session)
        await repo.create(
            tenant_id=tenant.id, email="bob@acme.com", role="viewer",
        )
        items, total = await repo.list_by_tenant(tenant.id, role="operator")
        assert total == 1
        assert items[0].email == "alice@acme.com"

    async def test_count_by_tenant(self, session, tenant, user):
        repo = UserRepo(session)
        assert await repo.count_by_tenant(tenant.id) == 1

    async def test_update(self, session, user):
        repo = UserRepo(session)
        updated = await repo.update(user, display_name="Alice Smith")
        assert updated.display_name == "Alice Smith"


class TestCustomRoleRepo:
    async def test_create(self, session, tenant):
        repo = CustomRoleRepo(session)
        role = await repo.create(
            tenant_id=tenant.id, name="shift-lead",
            permissions=["fleet.view", "incident.view"],
            created_by="admin-user",
        )
        assert role.name == "shift-lead"
        assert role.permissions == ["fleet.view", "incident.view"]

    async def test_list_by_tenant(self, session, tenant):
        repo = CustomRoleRepo(session)
        await repo.create(
            tenant_id=tenant.id, name="role-a",
            permissions=["fleet.view"], created_by="admin",
        )
        await repo.create(
            tenant_id=tenant.id, name="role-b",
            permissions=["incident.view"], created_by="admin",
        )
        roles = await repo.list_by_tenant(tenant.id)
        assert len(roles) == 2

    async def test_update(self, session, tenant):
        repo = CustomRoleRepo(session)
        role = await repo.create(
            tenant_id=tenant.id, name="shift-lead",
            permissions=["fleet.view"], created_by="admin",
        )
        updated = await repo.update(role, permissions=["fleet.view", "action.approve"])
        assert "action.approve" in updated.permissions

    async def test_delete(self, session, tenant):
        repo = CustomRoleRepo(session)
        role = await repo.create(
            tenant_id=tenant.id, name="temp-role",
            permissions=[], created_by="admin",
        )
        await repo.delete(role)
        assert await repo.get_by_id(role.id) is None

    async def test_unique_name_per_tenant(self, session, tenant):
        repo = CustomRoleRepo(session)
        await repo.create(
            tenant_id=tenant.id, name="shift-lead",
            permissions=[], created_by="admin",
        )
        await session.commit()
        with pytest.raises(IntegrityError):
            await repo.create(
                tenant_id=tenant.id, name="shift-lead",
                permissions=[], created_by="admin",
            )


class TestAuditRepo:
    async def test_append(self, session, tenant):
        repo = AuditRepo(session)
        row = await repo.append(
            actor_id="u1", actor_email="admin@acme.com",
            action="tenant.create", subject_type="tenant",
            subject_id=tenant.id, tenant_id=tenant.id,
            detail={"note": "first"},
        )
        assert row.action == "tenant.create"
        assert row.detail == {"note": "first"}

    async def test_list_filtered(self, session, tenant):
        repo = AuditRepo(session)
        await repo.append(
            actor_id="u1", actor_email="admin@acme.com",
            action="tenant.create", tenant_id=tenant.id,
        )
        await repo.append(
            actor_id="u2", actor_email="op@acme.com",
            action="user.invite", tenant_id=tenant.id,
        )
        items, total = await repo.list_filtered(tenant_id=tenant.id)
        assert total == 2

    async def test_list_by_actor(self, session, tenant):
        repo = AuditRepo(session)
        await repo.append(
            actor_id="u1", actor_email="admin@acme.com",
            action="tenant.create", tenant_id=tenant.id,
        )
        await repo.append(
            actor_id="u2", actor_email="op@acme.com",
            action="user.invite", tenant_id=tenant.id,
        )
        items, total = await repo.list_filtered(tenant_id=tenant.id, actor="admin")
        assert total == 1
        assert items[0].actor_email == "admin@acme.com"

    async def test_list_by_date_range(self, session, tenant):
        repo = AuditRepo(session)
        await repo.append(
            actor_id="u1", actor_email="admin@acme.com",
            action="tenant.create", tenant_id=tenant.id,
        )
        t = now()
        items, total = await repo.list_filtered(
            tenant_id=tenant.id,
            date_from=t - timedelta(minutes=1),
            date_to=t + timedelta(minutes=1),
        )
        assert total == 1


class TestSettingsRepo:
    async def test_get_set(self, session):
        repo = SettingsRepo(session)
        setting = await repo.set(
            "billing.currency", {"default": "USD"}, updated_by="admin",
        )
        assert setting.value == {"default": "USD"}
        found = await repo.get("billing.currency")
        assert found is not None
        assert found.value == {"default": "USD"}

    async def test_overwrite(self, session):
        repo = SettingsRepo(session)
        await repo.set("billing.currency", {"default": "USD"})
        updated = await repo.set("billing.currency", {"default": "INR"})
        assert updated.value == {"default": "INR"}
