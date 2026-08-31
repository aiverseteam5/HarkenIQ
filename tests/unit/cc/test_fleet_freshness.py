"""The freshness contract: last_seen_at is the SITE's reading, not CC's clock.

Extracted from the closed PR #6, which found the defect: the Site Manager has
always sent ``FleetDevice.last_seen_unix`` and ``SMClient`` has always
dictified it, but ``fleet_poller`` dropped it and ``cc_fleet_cache`` had no
column, so ``/api/fleet/{id}`` served ``snapshot_at`` — CC's own cache-refresh
time — under the name ``last_seen_at``. A silent agent looked fresh on every
poll.

The wire test is the load-bearing one. This is the ninth instance in this
repo of a value crossing proto -> dict -> repo with nothing reading it on the
far side, and the only test shape that catches that class is one that starts
at a real servicer and ends at the HTTP response.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import grpc
import pytest
from httpx import ASGITransport, AsyncClient

from harkeniq.proto import harkeniq_pb2_grpc
from harkeniq_cc.api.fleet import _device_dict
from harkeniq_cc.app import create_app
from harkeniq_cc.auth import configure_auth
from harkeniq_cc.config import CCConfig
from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_cc.db.repos import FleetCacheRepo, SiteRepo
from harkeniq_cc.fleet_poller import _last_seen
from harkeniq_cc.sm_client import SMClient
from harkeniq_sm.approvals import ApprovalService
from harkeniq_sm.config import SMConfig
from harkeniq_sm.db.base import create_all as sm_create_all
from harkeniq_sm.db.base import make_engine as sm_make_engine
from harkeniq_sm.db.base import make_sessionmaker as sm_make_sessionmaker
from harkeniq_sm.db.models import Device, Site
from harkeniq_sm.grpc_server import SiteManagerServiceServicer
from harkeniq_cc.runtime import AppState

TENANT = "tenant-fresh"
CC_SITE_ID = "cc-site-fresh"
# Deliberately in the past, so it cannot be confused with any utcnow() the
# ingest path might substitute.
SEEN_AT = datetime(2026, 8, 30, 9, 30, 0, tzinfo=timezone.utc)


def _instant(iso: str) -> datetime:
    """Parse an API timestamp as an instant.

    sqlite drops tzinfo on read-back where PostgreSQL keeps it, so comparing
    the ISO strings would assert the storage engine, not the contract.
    """
    dt = datetime.fromisoformat(iso)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class TestLastSeenConversion:
    def test_unix_becomes_an_aware_datetime(self):
        got = _last_seen({"last_seen_unix": int(SEEN_AT.timestamp())})
        assert got == SEEN_AT
        assert got.tzinfo is not None

    def test_missing_reading_is_none(self):
        assert _last_seen({}) is None

    def test_zero_is_none_not_the_epoch(self):
        """An SM sends 0 for a device it has never heard from.

        Converting that to 1970 would render as a real (very stale) reading
        instead of an honest blank.
        """
        assert _last_seen({"last_seen_unix": 0}) is None


class TestDeviceDictCarriesBothStamps:
    async def test_both_present_and_distinct(self, session):
        site = await SiteRepo(session).upsert(TENANT, "s1", "sm:50051")
        dev = await FleetCacheRepo(session).upsert_device(
            site.id, "agent-1", last_seen_at=SEEN_AT,
        )
        d = _device_dict(dev)
        assert _instant(d["last_seen_at"]) == SEEN_AT
        assert d["snapshot_at"] != d["last_seen_at"]

    async def test_never_seen_is_null_not_the_refresh_time(self, session):
        site = await SiteRepo(session).upsert(TENANT, "s1", "sm:50051")
        dev = await FleetCacheRepo(session).upsert_device(site.id, "agent-1")
        d = _device_dict(dev)
        assert d["last_seen_at"] is None
        assert d["snapshot_at"] is not None


@pytest.fixture
async def sm_wire():
    """Real SM servicer with a device whose last_seen_at is a known past."""
    engine = sm_make_engine("sqlite+aiosqlite:///:memory:")
    await sm_create_all(engine)
    db = sm_make_sessionmaker(engine)
    async with db() as session:
        site = Site(name="site-1", cc_site_id=CC_SITE_ID)
        session.add(site)
        await session.flush()
        session.add(Device(
            id="dev-x", site_id=site.id, agent_id="agent-1",
            agent_name="srv-01", vendor="dell", model="R750",
            last_seen_at=SEEN_AT,
        ))
        await session.commit()

    config = SMConfig(insecure=True, site_name="site-1")
    servicer = SiteManagerServiceServicer(db, ApprovalService(db, config), config)
    server = grpc.aio.server()
    harkeniq_pb2_grpc.add_SiteManagerServiceServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    yield f"127.0.0.1:{port}"
    await server.stop(grace=None)
    await engine.dispose()


async def test_reading_survives_sm_to_proto_to_dict_to_repo_to_http(sm_wire):
    """The whole path, because every hop in it has dropped a field before.

    SM row -> FleetDevice.last_seen_unix -> SMClient dict -> _last_seen ->
    upsert_device -> cc_fleet_cache -> /api/fleet/{id}.
    """
    snapshot = await SMClient().get_fleet_snapshot(
        sm_wire, "any-token", TENANT, CC_SITE_ID,
    )
    device = snapshot["devices"][0]
    assert device["last_seen_unix"] == int(SEEN_AT.timestamp())

    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    sessionmaker = make_sessionmaker(engine)
    config = CCConfig(tenant_id=TENANT, insecure=True)
    configure_auth("", "", "", insecure=True)
    state = AppState(config=config, engine=engine, sessionmaker=sessionmaker)
    app = create_app(state)

    async with sessionmaker() as session:
        site = await SiteRepo(session).upsert(TENANT, "site-1", "sm:50051")
        row = await FleetCacheRepo(session).upsert_device(
            site_id=site.id,
            agent_id=device["agent_id"],
            agent_name=device.get("agent_name", ""),
            vendor=device.get("vendor", ""),
            model=device.get("model", ""),
            last_seen_at=_last_seen(device),
        )
        device_id = row.id
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get(f"/api/fleet/{device_id}")
        assert r.status_code == 200
        detail = r.json()

    assert _instant(detail["last_seen_at"]) == SEEN_AT
    # The bug, pinned: the detail route used to overwrite last_seen_at with
    # snapshot_at, which is written at ingest and is therefore ~now.
    assert detail["snapshot_at"] != detail["last_seen_at"]
    refreshed = _instant(detail["snapshot_at"])
    assert refreshed - SEEN_AT > timedelta(hours=1)

    await engine.dispose()
