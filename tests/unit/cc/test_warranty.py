"""Warranty/lifecycle tests (R4-2 P15).

Covers: status derivation, the TTL cache repo, the Dell TechDirect
adapter (mocked httpx -- token flow, entitlement parsing, batching,
failure modes), the refresh loop, and the fleet API surfaces the
Console dashboard reads (list rows + the new /api/fleet/{id} detail).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from harkeniq_cc.app import create_app
from harkeniq_cc.auth import configure_auth
from harkeniq_cc.config import CCConfig
from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_cc.db.models import CCWarranty
from harkeniq_cc.db.repos import FleetCacheRepo, SiteRepo, WarrantyRepo
from harkeniq_cc.runtime import AppState
from harkeniq_cc.warranty.base import (
    MockWarrantyProvider,
    WarrantyRecord,
    warranty_status,
)
from harkeniq_cc.warranty.dell_techdirect import DellTechDirectProvider
from harkeniq_cc.warranty.refresh import make_provider, refresh_once

TENANT = "test-tenant"


def _iso(days_from_now: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days_from_now)).strftime(
        "%Y-%m-%d"
    )


class TestWarrantyStatus:
    def test_active(self):
        assert warranty_status(_iso(365)) == "active"

    def test_expiring_within_90_days(self):
        assert warranty_status(_iso(30)) == "expiring"

    def test_expired(self):
        assert warranty_status(_iso(-10)) == "expired"

    def test_unknown(self):
        assert warranty_status("") == "unknown"
        assert warranty_status("not-a-date") == "unknown"


class TestWarrantyRepo:
    async def test_upsert_and_get_map(self, session):
        repo = WarrantyRepo(session)
        n = await repo.upsert_records([
            WarrantyRecord("TAG1", "dell", "ProSupport", "2024-01-01",
                           _iso(365), "dell_techdirect"),
        ])
        await session.commit()
        assert n == 1
        found = await repo.get_map(["TAG1", "TAG2"])
        assert set(found) == {"TAG1"}
        assert found["TAG1"].service_level == "ProSupport"

    async def test_stale_or_missing(self, session):
        repo = WarrantyRepo(session)
        await repo.upsert_records([
            WarrantyRecord("FRESH", "dell", end_date=_iso(100)),
        ])
        old = CCWarranty(service_tag="STALE", vendor="dell",
                         fetched_at=datetime.now(timezone.utc) - timedelta(days=30))
        session.add(old)
        await session.commit()
        stale = await repo.stale_or_missing_tags(
            ["FRESH", "STALE", "MISSING"], ttl_s=7 * 86400,
        )
        assert sorted(stale) == ["MISSING", "STALE"]


def _mock_http(responses: list):
    """AsyncClient mock whose post/get return queued responses."""
    client = AsyncMock()
    post_resp, *get_resps = responses
    client.post = AsyncMock(return_value=post_resp)
    client.get = AsyncMock(side_effect=get_resps)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


def _resp(json_data, status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.raise_for_status = MagicMock()
    resp.json.return_value = json_data
    return resp


class TestDellTechDirectProvider:
    async def test_token_and_entitlement_parse(self):
        provider = DellTechDirectProvider("cid", "secret")
        assets = [{
            "serviceTag": "ABC123",
            "entitlements": [
                {"serviceLevelDescription": "Basic", "startDate": "2023-01-01",
                 "endDate": "2024-01-01"},
                {"serviceLevelDescription": "ProSupport Plus",
                 "startDate": "2023-01-01", "endDate": "2027-06-30"},
            ],
        }]
        client = _mock_http([
            _resp({"access_token": "tok", "expires_in": 3600}),
            _resp(assets),
        ])
        with patch("httpx.AsyncClient", return_value=client):
            records = await provider.fetch(["ABC123"])
        assert len(records) == 1
        r = records[0]
        assert r.service_tag == "ABC123"
        # Latest endDate entitlement wins
        assert r.service_level == "ProSupport Plus"
        assert r.end_date == "2027-06-30"
        assert r.source == "dell_techdirect"
        # Bearer token used
        headers = client.get.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer tok"

    async def test_batching_over_100_tags(self):
        provider = DellTechDirectProvider("cid", "secret")
        client = _mock_http([
            _resp({"access_token": "tok", "expires_in": 3600}),
            _resp([]), _resp([]),
        ])
        with patch("httpx.AsyncClient", return_value=client):
            await provider.fetch([f"TAG{i}" for i in range(150)])
        assert client.get.call_count == 2
        first_params = client.get.call_args_list[0].kwargs["params"]
        assert len(first_params["servicetags"].split(",")) == 100

    async def test_token_failure_returns_empty(self):
        provider = DellTechDirectProvider("cid", "bad-secret")
        token_resp = _resp({}, status=401)
        token_resp.raise_for_status = MagicMock(side_effect=Exception("401"))
        client = _mock_http([token_resp])
        with patch("httpx.AsyncClient", return_value=client):
            assert await provider.fetch(["ABC123"]) == []

    async def test_batch_failure_partial_results(self):
        provider = DellTechDirectProvider("cid", "secret")
        bad = _resp({}, status=500)
        bad.raise_for_status = MagicMock(side_effect=Exception("500"))
        good = _resp([{"serviceTag": "T2", "entitlements": [
            {"endDate": "2027-01-01", "serviceLevelDescription": "Basic"}]}])
        client = _mock_http([
            _resp({"access_token": "tok", "expires_in": 3600}), bad, good,
        ])
        with patch("httpx.AsyncClient", return_value=client):
            records = await provider.fetch(
                [f"A{i}" for i in range(100)] + ["T2"]
            )
        assert [r.service_tag for r in records] == ["T2"]


class TestRefreshLoop:
    async def test_provider_gating(self):
        assert make_provider(CCConfig()) is None
        provider = make_provider(CCConfig(
            dell_api_client_id="cid", dell_api_client_secret="secret",
        ))
        assert provider is not None and provider.name == "dell_techdirect"

    async def test_refresh_once_fetches_only_stale_dell_tags(self, db):
        async with db() as session:
            site = await SiteRepo(session).upsert(TENANT, "dc-1", "sm:50051")
            cache = FleetCacheRepo(session)
            await cache.upsert_device(site_id=site.id, agent_id="a1",
                                      vendor="dell", service_tag="DTAG1")
            await cache.upsert_device(site_id=site.id, agent_id="a2",
                                      vendor="hpe", service_tag="HTAG1")
            # Fresh cache entry for a second dell device
            await cache.upsert_device(site_id=site.id, agent_id="a3",
                                      vendor="dell", service_tag="DTAG2")
            await WarrantyRepo(session).upsert_records([
                WarrantyRecord("DTAG2", "dell", end_date=_iso(200)),
            ], tenant_id=TENANT)
            await session.commit()

        provider = MockWarrantyProvider({
            "DTAG1": WarrantyRecord("DTAG1", "dell", "ProSupport",
                                    "2024-01-01", _iso(400), "mock"),
        })
        # Exercise the Dell-only tag filter (applies to the Dell provider)
        provider.name = "dell_techdirect"
        state = AppState(
            config=CCConfig(tenant_id=TENANT, insecure=True,
                            warranty_ttl_s=7 * 86400),
            sessionmaker=db,
        )
        updated = await refresh_once(state, provider)
        assert updated == 1
        # Only the stale dell tag was requested -- not hpe, not the fresh one
        assert provider.calls == [["DTAG1"]]

        # Second cycle: nothing stale, provider untouched
        updated = await refresh_once(state, provider)
        assert updated == 0
        assert len(provider.calls) == 1


@pytest.fixture
async def client():
    config = CCConfig(tenant_id=TENANT, insecure=True)
    configure_auth("", "", "", insecure=True)
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    sessionmaker = make_sessionmaker(engine)
    state = AppState(config=config, engine=engine, sessionmaker=sessionmaker)
    app = create_app(state)

    async with sessionmaker() as session:
        site = await SiteRepo(session).upsert(TENANT, "dc-blr-1", "sm:50051")
        row = await FleetCacheRepo(session).upsert_device(
            site_id=site.id, agent_id="agent-1", agent_name="srv-01",
            vendor="dell", model="R750", service_tag="DTAG1",
            firmware=[{"component": "bmc", "name": "iDRAC9",
                       "version": "7.00.00.00"}],
            # A real site reading. This used to be unset and the assertion
            # below still passed, because the API aliased snapshot_at (CC's
            # own cache refresh) to last_seen_at.
            last_seen_at=datetime(2026, 8, 28, 9, 30, tzinfo=timezone.utc),
        )
        device_id = row.id
        await WarrantyRepo(session).upsert_records([
            WarrantyRecord("DTAG1", "dell", "ProSupport Plus",
                           "2024-01-01", _iso(400), "dell_techdirect"),
        ], tenant_id=TENANT)
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.device_id = device_id
        yield c
    await engine.dispose()


class TestWarrantyAPI:
    async def test_import_and_list(self, client):
        r = await client.post("/api/warranty/import", json={"records": [
            {"service_tag": "HTAG1", "vendor": "hpe",
             "service_level": "Foundation Care", "end_date": _iso(-5)},
        ]})
        assert r.status_code == 200
        assert r.json()["imported"] == 1
        r = await client.get("/api/warranty/")
        records = {x["service_tag"]: x for x in r.json()["records"]}
        assert records["HTAG1"]["source"] == "import"
        assert records["HTAG1"]["status"] == "expired"
        assert records["DTAG1"]["status"] == "active"

    async def test_fleet_list_includes_warranty(self, client):
        r = await client.get("/api/fleet/")
        assert r.status_code == 200
        data = r.json()
        assert data["items"] == data["devices"]  # UI-shape alias
        device = data["devices"][0]
        assert device["service_tag"] == "DTAG1"
        assert device["warranty"]["status"] == "active"
        assert device["warranty"]["service_level"] == "ProSupport Plus"

    async def test_device_detail_endpoint(self, client):
        r = await client.get(f"/api/fleet/{client.device_id}")
        assert r.status_code == 200
        detail = r.json()
        assert detail["name"] == "srv-01"
        assert detail["site_name"] == "dc-blr-1"
        assert detail["warranty"]["status"] == "active"
        assert detail["firmware"][0]["version"] == "7.00.00.00"
        # The site's reading, not CC's cache refresh — they are separate
        # fields now and only the seeded one is asserted here.
        assert detail["last_seen_at"].startswith("2026-08-28T09:30")

    async def test_device_detail_404(self, client):
        r = await client.get("/api/fleet/nonexistent-id")
        assert r.status_code == 404

    async def test_incident_routes_not_shadowed(self, client):
        r = await client.get("/api/fleet/incidents")
        assert r.status_code == 200
        assert "incidents" in r.json()
