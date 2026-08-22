"""Phase 7 repository tests: ApiKey, ImpersonationLog."""

from __future__ import annotations

import hashlib
import secrets
from datetime import date, datetime, timedelta, timezone

import pytest

from harkeniq_console.db.repos import (
    ApiKeyRepo,
    ImpersonationLogRepo,
    TenantRepo,
)


def utcnow():
    return datetime.now(timezone.utc)


@pytest.fixture
async def tenant(session):
    return await TenantRepo(session).create(
        slug="acme", name="Acme Corp", billing_country="US",
    )


@pytest.fixture
async def second_tenant(session):
    return await TenantRepo(session).create(
        slug="globex", name="Globex Corp", billing_country="IN",
    )


def _make_key_hash():
    raw = f"hiq_{secrets.token_hex(16)}"
    return raw, hashlib.sha256(raw.encode()).hexdigest(), raw[:12]


# ── ApiKeyRepo ───────────────────────────────────────────────────────


class TestApiKeyRepo:
    async def test_create(self, session, tenant):
        raw, key_hash, prefix = _make_key_hash()
        repo = ApiKeyRepo(session)
        key = await repo.create(
            tenant_id=tenant.id, name="CI/CD", key_hash=key_hash,
            key_prefix=prefix, scope="write", created_by="u1",
        )
        assert key.id
        assert key.status == "active"
        assert key.scope == "write"

    async def test_get_by_id(self, session, tenant):
        _, key_hash, prefix = _make_key_hash()
        repo = ApiKeyRepo(session)
        key = await repo.create(
            tenant_id=tenant.id, name="Test", key_hash=key_hash,
            key_prefix=prefix, scope="read", created_by="u1",
        )
        found = await repo.get_by_id(key.id)
        assert found is not None
        assert found.name == "Test"

    async def test_get_by_id_not_found(self, session):
        assert await ApiKeyRepo(session).get_by_id("nope") is None

    async def test_get_by_hash(self, session, tenant):
        _, key_hash, prefix = _make_key_hash()
        repo = ApiKeyRepo(session)
        await repo.create(
            tenant_id=tenant.id, name="Lookup", key_hash=key_hash,
            key_prefix=prefix, scope="read", created_by="u1",
        )
        found = await repo.get_by_hash(key_hash)
        assert found is not None
        assert found.name == "Lookup"

    async def test_get_by_hash_not_found(self, session):
        assert await ApiKeyRepo(session).get_by_hash("nonexistent") is None

    async def test_list_by_tenant(self, session, tenant):
        repo = ApiKeyRepo(session)
        for i in range(3):
            _, h, p = _make_key_hash()
            await repo.create(
                tenant_id=tenant.id, name=f"Key{i}", key_hash=h,
                key_prefix=p, scope="read", created_by="u1",
            )
        items, total = await repo.list_by_tenant(tenant.id)
        assert total == 3

    async def test_list_by_tenant_status_filter(self, session, tenant):
        repo = ApiKeyRepo(session)
        _, h1, p1 = _make_key_hash()
        await repo.create(tenant_id=tenant.id, name="Active", key_hash=h1, key_prefix=p1, scope="read", created_by="u1")
        _, h2, p2 = _make_key_hash()
        key2 = await repo.create(tenant_id=tenant.id, name="Revoked", key_hash=h2, key_prefix=p2, scope="read", created_by="u1")
        await repo.revoke(key2)

        items, total = await repo.list_by_tenant(tenant.id, status="active")
        assert total == 1
        assert items[0].name == "Active"

    async def test_revoke(self, session, tenant):
        _, key_hash, prefix = _make_key_hash()
        repo = ApiKeyRepo(session)
        key = await repo.create(
            tenant_id=tenant.id, name="ToRevoke", key_hash=key_hash,
            key_prefix=prefix, scope="admin", created_by="u1",
        )
        revoked = await repo.revoke(key)
        assert revoked.status == "revoked"
        assert revoked.revoked_at is not None

    async def test_update_last_used(self, session, tenant):
        _, key_hash, prefix = _make_key_hash()
        repo = ApiKeyRepo(session)
        key = await repo.create(
            tenant_id=tenant.id, name="Used", key_hash=key_hash,
            key_prefix=prefix, scope="read", created_by="u1",
        )
        now = utcnow()
        updated = await repo.update(key, last_used_at=now)
        assert updated.last_used_at is not None

    async def test_expiration(self, session, tenant):
        _, key_hash, prefix = _make_key_hash()
        repo = ApiKeyRepo(session)
        key = await repo.create(
            tenant_id=tenant.id, name="Expiring", key_hash=key_hash,
            key_prefix=prefix, scope="read", created_by="u1",
            expires_at=utcnow() + timedelta(days=30),
        )
        assert key.expires_at is not None

    async def test_tenant_isolation(self, session, tenant, second_tenant):
        repo = ApiKeyRepo(session)
        _, h1, p1 = _make_key_hash()
        await repo.create(tenant_id=tenant.id, name="T1Key", key_hash=h1, key_prefix=p1, scope="read", created_by="u1")
        _, h2, p2 = _make_key_hash()
        await repo.create(tenant_id=second_tenant.id, name="T2Key", key_hash=h2, key_prefix=p2, scope="read", created_by="u2")

        items, total = await repo.list_by_tenant(tenant.id)
        assert total == 1
        assert items[0].name == "T1Key"


# ── ImpersonationLogRepo ─────────────────────────────────────────────


class TestImpersonationLogRepo:
    async def test_create(self, session, tenant):
        repo = ImpersonationLogRepo(session)
        entry = await repo.create(
            admin_user_id="admin1", admin_email="admin@harkeniq.com",
            tenant_id=tenant.id,
        )
        assert entry.id
        assert entry.ended_at is None
        assert entry.actions_count == 0

    async def test_get_by_id(self, session, tenant):
        repo = ImpersonationLogRepo(session)
        entry = await repo.create(
            admin_user_id="a1", admin_email="a@h.com", tenant_id=tenant.id,
        )
        found = await repo.get_by_id(entry.id)
        assert found is not None

    async def test_end_session(self, session, tenant):
        repo = ImpersonationLogRepo(session)
        entry = await repo.create(
            admin_user_id="a1", admin_email="a@h.com", tenant_id=tenant.id,
        )
        ended = await repo.end_session(entry)
        assert ended.ended_at is not None

    async def test_list_filtered(self, session, tenant, second_tenant):
        repo = ImpersonationLogRepo(session)
        await repo.create(admin_user_id="a1", admin_email="a1@h.com", tenant_id=tenant.id)
        await repo.create(admin_user_id="a2", admin_email="a2@h.com", tenant_id=second_tenant.id)
        await repo.create(admin_user_id="a1", admin_email="a1@h.com", tenant_id=second_tenant.id)

        # all
        items, total = await repo.list_filtered()
        assert total == 3

        # by admin
        items, total = await repo.list_filtered(admin_user_id="a1")
        assert total == 2

        # by tenant
        items, total = await repo.list_filtered(tenant_id=tenant.id)
        assert total == 1

    async def test_list_filtered_date_range(self, session, tenant):
        repo = ImpersonationLogRepo(session)
        await repo.create(admin_user_id="a1", admin_email="a@h.com", tenant_id=tenant.id)
        now = utcnow()

        items, total = await repo.list_filtered(
            date_from=now - timedelta(hours=1),
            date_to=now + timedelta(hours=1),
        )
        assert total == 1

    async def test_list_pagination(self, session, tenant):
        repo = ImpersonationLogRepo(session)
        for i in range(5):
            await repo.create(admin_user_id=f"a{i}", admin_email=f"a{i}@h.com", tenant_id=tenant.id)

        items, total = await repo.list_filtered(page=1, page_size=3)
        assert total == 5
        assert len(items) == 3

        items2, _ = await repo.list_filtered(page=2, page_size=3)
        assert len(items2) == 2
