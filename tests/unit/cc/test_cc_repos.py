"""Central Command repository behavior against in-memory aiosqlite."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from harkeniq_cc.db.repos import (
    ApprovalRouteRepo,
    AuditRepo,
    FleetCacheRepo,
    SiteRepo,
    UsageSnapshotRepo,
)


def now():
    return datetime.now(timezone.utc)


TENANT = "tenant-test"


@pytest.fixture
async def site(session):
    repo = SiteRepo(session)
    return await repo.upsert(TENANT, "site-1", "https://sm1.lab:50051")


class TestSiteRepo:
    async def test_create_site(self, session):
        repo = SiteRepo(session)
        site = await repo.upsert(TENANT, "dc-blr-1", "https://sm.lab:50051")
        assert site.tenant_id == TENANT
        assert site.site_name == "dc-blr-1"
        assert site.sm_endpoint == "https://sm.lab:50051"
        assert site.status == "active"

    async def test_get_by_id(self, session, site):
        repo = SiteRepo(session)
        found = await repo.get_by_id(site.id)
        assert found is not None
        assert found.site_name == "site-1"

    async def test_get_by_name(self, session, site):
        repo = SiteRepo(session)
        found = await repo.get_by_name(TENANT, "site-1")
        assert found is not None
        assert found.id == site.id

    async def test_list_all(self, session, site):
        repo = SiteRepo(session)
        await repo.upsert(TENANT, "site-2", "https://sm2.lab:50051")
        sites = await repo.list_all(TENANT)
        assert len(sites) == 2
        assert [s.site_name for s in sites] == ["site-1", "site-2"]

    async def test_update_last_seen(self, session, site):
        repo = SiteRepo(session)
        old_seen = site.last_seen_at
        # Small sleep alternative: just verify the method runs without error
        await repo.update_last_seen(site)
        assert site.last_seen_at >= old_seen

    async def test_unique_constraint(self, session, site):
        """Upsert same name returns same row, not duplicate."""
        repo = SiteRepo(session)
        again = await repo.upsert(TENANT, "site-1", "https://sm1-new.lab:50051")
        assert again.id == site.id
        assert again.sm_endpoint == "https://sm1-new.lab:50051"


class TestFleetCacheRepo:
    async def test_upsert_device(self, session, site):
        repo = FleetCacheRepo(session)
        dev = await repo.upsert_device(
            site.id, "agent-1", agent_name="rack-12-srv-04",
            vendor="Dell", model="R750", observation="observed", health="ok",
        )
        assert dev.agent_id == "agent-1"
        assert dev.vendor == "Dell"

    async def test_list_by_site(self, session, site):
        repo = FleetCacheRepo(session)
        await repo.upsert_device(site.id, "agent-1", agent_name="srv-01")
        await repo.upsert_device(site.id, "agent-2", agent_name="srv-02")
        devices = await repo.list_by_site(site.id)
        assert len(devices) == 2

    async def test_list_all_tenant(self, session, site):
        repo = FleetCacheRepo(session)
        await repo.upsert_device(site.id, "agent-1", agent_name="srv-01")
        devices = await repo.list_all(TENANT)
        assert len(devices) == 1
        assert devices[0].agent_id == "agent-1"

    async def test_clear_site(self, session, site):
        repo = FleetCacheRepo(session)
        await repo.upsert_device(site.id, "agent-1")
        await repo.upsert_device(site.id, "agent-2")
        await repo.clear_site(site.id)
        assert await repo.list_by_site(site.id) == []

    async def test_last_seen_defaults_to_null_not_to_snapshot(
        self, session, site
    ):
        """An unreported reading is unknown, never "now".

        The whole defect this column fixes was snapshot_at standing in for a
        reading nobody supplied, so the absent case has to stay absent.
        """
        repo = FleetCacheRepo(session)
        dev = await repo.upsert_device(site.id, "agent-1")
        assert dev.last_seen_at is None
        assert dev.snapshot_at is not None

    async def test_last_seen_is_stored_and_is_not_snapshot_at(
        self, session, site
    ):
        seen = datetime(2026, 8, 30, 12, 0, 0, tzinfo=timezone.utc)
        repo = FleetCacheRepo(session)
        dev = await repo.upsert_device(site.id, "agent-1", last_seen_at=seen)
        assert dev.last_seen_at == seen
        assert dev.snapshot_at != dev.last_seen_at

    async def test_poll_without_a_reading_keeps_the_last_honest_value(
        self, session, site
    ):
        """A later poll that carries no reading must not erase the old one.

        Clearing it would make a briefly-quiet SM look like an agent that had
        never been seen; overwriting it with utcnow() would be the original
        lie wearing the new column's name.
        """
        seen = datetime(2026, 8, 30, 12, 0, 0, tzinfo=timezone.utc)
        repo = FleetCacheRepo(session)
        await repo.upsert_device(site.id, "agent-1", last_seen_at=seen)
        dev = await repo.upsert_device(site.id, "agent-1", health="ok")
        # sqlite drops tzinfo on read-back where PostgreSQL keeps it, so
        # compare the instant rather than the storage engine's rendering.
        got = dev.last_seen_at
        if got.tzinfo is None:
            got = got.replace(tzinfo=timezone.utc)
        assert got == seen


class TestApprovalRouteRepo:
    async def test_create(self, session, site):
        repo = ApprovalRouteRepo(session)
        route = await repo.create(site.id, "act-1", action_type="fan_boost")
        assert route.action_id == "act-1"
        assert route.decision is None

    async def test_get_by_action_id(self, session, site):
        repo = ApprovalRouteRepo(session)
        await repo.create(site.id, "act-2", action_type="led_on")
        found = await repo.get_by_action_id("act-2")
        assert found is not None
        assert found.action_type == "led_on"

    async def test_list_pending(self, session, site):
        repo = ApprovalRouteRepo(session)
        await repo.create(site.id, "act-3")
        await repo.create(site.id, "act-4")
        pending = await repo.list_pending(TENANT)
        assert len(pending) == 2

    async def test_list_history(self, session, site):
        repo = ApprovalRouteRepo(session)
        route = await repo.create(site.id, "act-5")
        await repo.update_decision(route, "approved", "op@lab")
        history = await repo.list_history(TENANT)
        assert len(history) == 1
        assert history[0].decision == "approved"

    async def test_update_decision(self, session, site):
        repo = ApprovalRouteRepo(session)
        route = await repo.create(site.id, "act-6")
        await repo.update_decision(route, "denied", "admin@lab")
        assert route.decision == "denied"
        assert route.decided_by == "admin@lab"
        assert route.decided_at is not None


class TestUsageSnapshotRepo:
    async def test_upsert(self, session, site):
        repo = UsageSnapshotRepo(session)
        snap = await repo.upsert(
            "2026-08-20", site.id, TENANT,
            node_count=42, agent_versions={"0.1.0": 40, "0.2.0": 2},
        )
        assert snap.node_count == 42
        assert snap.agent_versions["0.1.0"] == 40

    async def test_get_by_month(self, session, site):
        repo = UsageSnapshotRepo(session)
        await repo.upsert("2026-08-01", site.id, TENANT, node_count=10)
        await repo.upsert("2026-08-15", site.id, TENANT, node_count=12)
        await repo.upsert("2026-09-01", site.id, TENANT, node_count=15)
        aug = await repo.get_by_month(TENANT, 2026, 8)
        assert len(aug) == 2

    async def test_get_by_date(self, session, site):
        repo = UsageSnapshotRepo(session)
        await repo.upsert("2026-08-20", site.id, TENANT, node_count=50)
        rows = await repo.get_by_date(TENANT, "2026-08-20")
        assert len(rows) == 1
        assert rows[0].node_count == 50


class TestAuditRepo:
    async def test_append(self, session):
        repo = AuditRepo(session)
        row = await repo.append(
            "admin", "site.register", subject="site-1",
            tenant_id=TENANT, detail={"note": "first site"},
        )
        assert row.actor == "admin"
        assert row.action == "site.register"
        assert row.detail == {"note": "first site"}

    async def test_list_filtered(self, session):
        repo = AuditRepo(session)
        await repo.append("admin", "site.register", tenant_id=TENANT)
        await repo.append("op", "action.approve", tenant_id=TENANT)
        rows = await repo.list_filtered(TENANT)
        assert len(rows) == 2

    async def test_list_by_date_range(self, session):
        repo = AuditRepo(session)
        await repo.append("admin", "site.register", tenant_id=TENANT)
        t = now()
        rows = await repo.list_filtered(
            TENANT,
            date_from=t - timedelta(minutes=1),
            date_to=t + timedelta(minutes=1),
        )
        assert len(rows) == 1
