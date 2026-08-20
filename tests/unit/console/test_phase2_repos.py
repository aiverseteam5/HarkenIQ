"""Phase 2 repository tests: License, Subscription, TenantSite, FeatureFlag."""

from datetime import date, datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from harkeniq_console.db.repos import (
    FeatureFlagRepo,
    LicenseRepo,
    SubscriptionRepo,
    TenantRepo,
    TenantSiteRepo,
)


def utcnow():
    return datetime.now(timezone.utc)


@pytest.fixture
async def tenant(session):
    repo = TenantRepo(session)
    return await repo.create(slug="acme", name="Acme Corp", billing_country="US")


@pytest.fixture
async def second_tenant(session):
    repo = TenantRepo(session)
    return await repo.create(slug="globex", name="Globex Corp", billing_country="IN")


# ── LicenseRepo ────────────────────────────────────────────────────

class TestLicenseRepo:
    async def test_create(self, session, tenant):
        repo = LicenseRepo(session)
        lic = await repo.create(
            tenant_id=tenant.id,
            license_key="signed-key-data",
            fingerprint="abc123",
            plan="approve",
            node_commit=100,
            valid_from=utcnow(),
            valid_until=utcnow(),
            issued_by="admin@harkeniq.com",
        )
        assert lic.id
        assert lic.status == "active"
        assert lic.plan == "approve"

    async def test_get_by_id(self, session, tenant):
        repo = LicenseRepo(session)
        lic = await repo.create(
            tenant_id=tenant.id,
            license_key="key1",
            fingerprint="fp1",
            plan="observe",
            node_commit=10,
            valid_from=utcnow(),
            valid_until=utcnow(),
        )
        found = await repo.get_by_id(lic.id)
        assert found is not None
        assert found.fingerprint == "fp1"

    async def test_get_by_id_not_found(self, session):
        repo = LicenseRepo(session)
        assert await repo.get_by_id("nonexistent") is None

    async def test_get_by_fingerprint(self, session, tenant):
        repo = LicenseRepo(session)
        await repo.create(
            tenant_id=tenant.id,
            license_key="key2",
            fingerprint="unique-fp",
            plan="approve",
            node_commit=50,
            valid_from=utcnow(),
            valid_until=utcnow(),
        )
        found = await repo.get_by_fingerprint("unique-fp")
        assert found is not None
        assert found.tenant_id == tenant.id

    async def test_list_by_tenant(self, session, tenant):
        repo = LicenseRepo(session)
        await repo.create(
            tenant_id=tenant.id, license_key="k1", fingerprint="f1",
            plan="approve", node_commit=100,
            valid_from=utcnow(), valid_until=utcnow(),
        )
        await repo.create(
            tenant_id=tenant.id, license_key="k2", fingerprint="f2",
            plan="observe", node_commit=10,
            valid_from=utcnow(), valid_until=utcnow(),
        )
        items, total = await repo.list_by_tenant(tenant.id)
        assert total == 2
        assert len(items) == 2

    async def test_list_by_tenant_filter_status(self, session, tenant):
        repo = LicenseRepo(session)
        lic1 = await repo.create(
            tenant_id=tenant.id, license_key="k1", fingerprint="f1",
            plan="approve", node_commit=100,
            valid_from=utcnow(), valid_until=utcnow(),
        )
        await repo.create(
            tenant_id=tenant.id, license_key="k2", fingerprint="f2",
            plan="observe", node_commit=10,
            valid_from=utcnow(), valid_until=utcnow(),
        )
        await repo.revoke(lic1, revoked_by="admin", reason="test")
        items, total = await repo.list_by_tenant(tenant.id, status="active")
        assert total == 1
        items_rev, total_rev = await repo.list_by_tenant(tenant.id, status="revoked")
        assert total_rev == 1

    async def test_revoke(self, session, tenant):
        repo = LicenseRepo(session)
        lic = await repo.create(
            tenant_id=tenant.id, license_key="k1", fingerprint="f-rev",
            plan="approve", node_commit=100,
            valid_from=utcnow(), valid_until=utcnow(),
        )
        await repo.revoke(lic, revoked_by="admin@hiq.com", reason="policy violation")
        assert lic.status == "revoked"
        assert lic.revoked_by == "admin@hiq.com"
        assert lic.revoke_reason == "policy violation"
        assert lic.revoked_at is not None

    async def test_update(self, session, tenant):
        repo = LicenseRepo(session)
        lic = await repo.create(
            tenant_id=tenant.id, license_key="k1", fingerprint="f-upd",
            plan="observe", node_commit=10,
            valid_from=utcnow(), valid_until=utcnow(),
        )
        updated = await repo.update(lic, plan="approve", node_commit=200)
        assert updated.plan == "approve"
        assert updated.node_commit == 200


# ── SubscriptionRepo ───────────────────────────────────────────────

class TestSubscriptionRepo:
    async def test_create(self, session, tenant):
        repo = SubscriptionRepo(session)
        sub = await repo.create(
            tenant_id=tenant.id,
            plan="approve",
            node_commit=100,
            billing_cycle_start=date(2026, 8, 1),
        )
        assert sub.id
        assert sub.plan == "approve"
        assert sub.status == "active"

    async def test_get_by_tenant(self, session, tenant):
        repo = SubscriptionRepo(session)
        await repo.create(
            tenant_id=tenant.id,
            plan="observe",
            node_commit=10,
            billing_cycle_start=date(2026, 8, 1),
        )
        found = await repo.get_by_tenant(tenant.id)
        assert found is not None
        assert found.plan == "observe"

    async def test_get_by_tenant_not_found(self, session, tenant):
        repo = SubscriptionRepo(session)
        found = await repo.get_by_tenant(tenant.id)
        assert found is None

    async def test_update(self, session, tenant):
        repo = SubscriptionRepo(session)
        sub = await repo.create(
            tenant_id=tenant.id,
            plan="observe",
            node_commit=10,
            billing_cycle_start=date(2026, 8, 1),
        )
        updated = await repo.update(sub, plan="approve", node_commit=200)
        assert updated.plan == "approve"
        assert updated.node_commit == 200
        assert updated.updated_at is not None


# ── TenantSiteRepo ─────────────────────────────────────────────────

class TestTenantSiteRepo:
    async def test_create(self, session, tenant):
        repo = TenantSiteRepo(session)
        site = await repo.create(
            tenant_id=tenant.id,
            site_name="us-east-1",
            sm_endpoint="sm.acme.com:50051",
        )
        assert site.id
        assert site.site_name == "us-east-1"
        assert site.status == "active"

    async def test_list_by_tenant(self, session, tenant):
        repo = TenantSiteRepo(session)
        await repo.create(tenant_id=tenant.id, site_name="us-east-1")
        await repo.create(tenant_id=tenant.id, site_name="us-west-2")
        sites = await repo.list_by_tenant(tenant.id)
        assert len(sites) == 2
        names = {s.site_name for s in sites}
        assert names == {"us-east-1", "us-west-2"}

    async def test_update(self, session, tenant):
        repo = TenantSiteRepo(session)
        site = await repo.create(
            tenant_id=tenant.id, site_name="us-east-1",
        )
        updated = await repo.update(site, status="decommissioned")
        assert updated.status == "decommissioned"

    async def test_unique_constraint(self, session, tenant):
        repo = TenantSiteRepo(session)
        await repo.create(tenant_id=tenant.id, site_name="us-east-1")
        await session.commit()
        with pytest.raises(IntegrityError):
            await repo.create(tenant_id=tenant.id, site_name="us-east-1")

    async def test_get_by_id(self, session, tenant):
        repo = TenantSiteRepo(session)
        site = await repo.create(tenant_id=tenant.id, site_name="eu-west-1")
        found = await repo.get_by_id(site.id)
        assert found is not None
        assert found.site_name == "eu-west-1"


# ── FeatureFlagRepo ────────────────────────────────────────────────

class TestFeatureFlagRepo:
    async def test_set_flag(self, session, tenant):
        repo = FeatureFlagRepo(session)
        flag = await repo.set_flag(
            tenant_id=tenant.id,
            feature_name="dark_mode",
            enabled=True,
            updated_by="admin",
        )
        assert flag.feature_name == "dark_mode"
        assert flag.enabled is True

    async def test_set_flag_update(self, session, tenant):
        repo = FeatureFlagRepo(session)
        await repo.set_flag(tenant.id, "dark_mode", True)
        updated = await repo.set_flag(tenant.id, "dark_mode", False)
        assert updated.enabled is False

    async def test_list_by_tenant(self, session, tenant):
        repo = FeatureFlagRepo(session)
        await repo.set_flag(tenant.id, "feat_a", True)
        await repo.set_flag(tenant.id, "feat_b", False)
        flags = await repo.list_by_tenant(tenant.id)
        assert len(flags) >= 2
        names = {f.feature_name for f in flags}
        assert "feat_a" in names
        assert "feat_b" in names

    async def test_get(self, session, tenant):
        repo = FeatureFlagRepo(session)
        await repo.set_flag(tenant.id, "beta_ui", True)
        flag = await repo.get(tenant.id, "beta_ui")
        assert flag is not None
        assert flag.enabled is True

    async def test_get_not_found(self, session, tenant):
        repo = FeatureFlagRepo(session)
        flag = await repo.get(tenant.id, "nonexistent")
        assert flag is None

    async def test_list_globals(self, session, tenant):
        repo = FeatureFlagRepo(session)
        # Global flags have tenant_id = None — we need to insert directly
        from harkeniq_console.db.models import FeatureFlag
        global_flag = FeatureFlag(
            tenant_id=None, feature_name="global_maintenance", enabled=True,
        )
        session.add(global_flag)
        await session.flush()
        # Tenant-specific flag
        await repo.set_flag(tenant.id, "tenant_feat", True)
        globals_ = await repo.list_globals()
        assert len(globals_) == 1
        assert globals_[0].feature_name == "global_maintenance"

    async def test_list_by_tenant_includes_globals(self, session, tenant):
        repo = FeatureFlagRepo(session)
        from harkeniq_console.db.models import FeatureFlag
        global_flag = FeatureFlag(
            tenant_id=None, feature_name="global_feat", enabled=True,
        )
        session.add(global_flag)
        await session.flush()
        await repo.set_flag(tenant.id, "tenant_feat", True)
        all_flags = await repo.list_by_tenant(tenant.id)
        names = {f.feature_name for f in all_flags}
        assert "global_feat" in names
        assert "tenant_feat" in names
