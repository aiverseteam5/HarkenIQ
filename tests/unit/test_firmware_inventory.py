"""Firmware inventory + CVE exposure tests (R4-2 P14, R-AGENT-17/18).

Covers: cross-vendor version comparison, range expressions, protocol
firmware collection (Redfish + IPMI), the agent-side collection at
startup, and the CC CVE feed import + exposure matching API.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from harkeniq.compliance.versions import (
    compare_versions,
    normalize_version,
    parse_version,
    version_in_range,
)
from harkeniq.mock.simulator import MockSimulator
from harkeniq.protocols.device import create_device_protocol

TENANT = "test-tenant"


class TestVersionParsing:
    def test_dotted_numeric(self):
        assert parse_version("7.00.00.00") == ((7, ""), (0, ""), (0, ""), (0, ""))

    def test_alpha_suffix(self):
        assert parse_version("2.5.6b") == ((2, ""), (5, ""), (6, "b"))

    def test_hpe_bios_string(self):
        # "U32 v2.68 (04/22/2026)" -> comparable core 2.68
        assert normalize_version("U32 v2.68 (04/22/2026)") == "2.68"
        assert compare_versions("U32 v2.68 (04/22/2026)", "2.68") == 0

    def test_non_numeric_version(self):
        # Drive firmware like "HPD0" parses without crashing
        assert parse_version("HPD0") == ((-1, "HPD0"),)


class TestVersionComparison:
    @pytest.mark.parametrize("a,b,expected", [
        ("1.0", "2.0", -1),
        ("2.0", "2.0", 0),
        ("2.10", "2.9", 1),          # numeric, not lexical
        ("7.00.00.00", "7.10.30.00", -1),
        ("2.5", "2.5.0", 0),         # shorter pads with zeros
        ("2.5.1", "2.5", 1),
        ("1.10.25.00", "1.9.99.99", 1),
        ("2.78", "2.80", -1),
        ("2.5.6a", "2.5.6b", -1),    # suffix ordering
    ])
    def test_compare(self, a, b, expected):
        assert compare_versions(a, b) == expected


class TestVersionRanges:
    @pytest.mark.parametrize("version,expr,expected", [
        ("7.00.00.00", "< 7.10.30.00", True),
        ("7.10.30.00", "< 7.10.30.00", False),
        ("2.78", "< 2.80", True),
        ("2.80", "< 2.80", False),
        ("1.5", ">= 1.0, < 2.0", True),
        ("2.0", ">= 1.0, < 2.0", False),
        ("3.1", "== 3.1", True),
        ("3.1", "!= 3.1", False),
        ("9.9", "*", True),
        ("1.0", "", False),
        ("", "< 2.0", False),
        ("1.0", "garbage", False),   # broken expr never matches
    ])
    def test_in_range(self, version, expr, expected):
        assert version_in_range(version, expr) is expected


@pytest.fixture
async def dell_sim():
    sim = MockSimulator(device="dell-r750", port=0, no_auth=True)
    await sim.start()
    yield sim
    await sim.stop()


class TestProtocolCollection:
    async def test_redfish_dell_inventory(self, dell_sim):
        proto = create_device_protocol("redfish", host=dell_sim.url)
        await proto.connect({"username": "admin", "password": "password"})
        try:
            inventory = await proto.collect_firmware_inventory()
        finally:
            await proto.disconnect()
        by_component = {}
        for entry in inventory:
            by_component.setdefault(entry["component"], []).append(entry)
        assert by_component["bmc"][0]["version"] == "7.00.00.00"
        assert by_component["bios"][0]["version"] == "1.15.2"
        assert len(by_component["psu"]) == 2
        assert by_component["psu"][0]["version"] == "00.24.67"

    async def test_ipmi_inventory_bmc_only(self):
        from harkeniq.mock.ipmi_sim import MockIPMIBMC
        from harkeniq.protocols.ipmi import IPMIProtocol

        bmc = MockIPMIBMC()
        proto = IPMIProtocol(host="10.0.0.1", backend_factory=bmc.factory())
        await proto.connect({"username": "admin", "password": "password"})
        inventory = await proto.collect_firmware_inventory()
        await proto.disconnect()
        assert len(inventory) == 1
        assert inventory[0]["component"] == "bmc"

    async def test_agent_collects_at_startup(self, dell_sim, tmp_path):
        from pathlib import Path

        from harkeniq.agent import Agent

        repo_root = Path(__file__).parents[2]
        agent = Agent({
            "bmc": {"host": dell_sim.url, "username": "admin",
                    "password": "password", "verify_ssl": False},
            "skills": {"directory": str(repo_root / "skills")},
            "polling": {"sensor_interval": 0.05},
        })
        await agent.start()
        try:
            components = {f["component"] for f in agent.firmware_inventory}
            assert components == {"bmc", "bios", "psu"}
        finally:
            await agent.stop()


@pytest.fixture
async def cc_client():
    from harkeniq_cc.app import create_app
    from harkeniq_cc.auth import configure_auth
    from harkeniq_cc.config import CCConfig
    from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
    from harkeniq_cc.db.repos import FleetCacheRepo, SiteRepo
    from harkeniq_cc.runtime import AppState

    config = CCConfig(tenant_id=TENANT, insecure=True)
    configure_auth("", "", "", insecure=True)
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    sessionmaker = make_sessionmaker(engine)
    state = AppState(config=config, engine=engine, sessionmaker=sessionmaker)
    app = create_app(state)
    # A23-5: a rowless tenant is STRICT now (A23.11), so this fixture
    # seeds the founding administrator tenant birth seeds (A23.14 D4)
    # instead of leaning on the `legacy_open` synthesis a missing row
    # used to give.
    from tests.unit.cc.conftest import seed_tenant_admin

    await seed_tenant_admin(sessionmaker, TENANT, "lab-user")

    async with sessionmaker() as session:
        site = await SiteRepo(session).upsert(
            TENANT, "dc-blr-1", "https://sm1.lab:50051"
        )
        cache = FleetCacheRepo(session)
        await cache.upsert_device(
            site_id=site.id, agent_id="agent-old", agent_name="srv-old",
            vendor="dell", model="R750", service_tag="TAG-OLD",
            firmware=[
                {"component": "bmc", "name": "iDRAC9", "version": "7.00.00.00"},
                {"component": "bios", "name": "BIOS", "version": "1.15.2"},
            ],
        )
        await cache.upsert_device(
            site_id=site.id, agent_id="agent-new", agent_name="srv-new",
            vendor="dell", model="R760", service_tag="TAG-NEW",
            firmware=[
                {"component": "bmc", "name": "iDRAC9", "version": "7.20.10.00"},
                {"component": "bios", "name": "BIOS", "version": "2.3.1"},
            ],
        )
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await engine.dispose()


FEED = {
    "entries": [
        {"cve_id": "EXAMPLE-2026-0001", "vendor": "dell", "component": "bmc",
         "affected_versions": "< 7.10.30.00", "fixed_version": "7.10.30.00",
         "severity": "high", "description": "old iDRAC vulnerable"},
        {"cve_id": "EXAMPLE-2026-0003", "vendor": "*", "component": "bios",
         "affected_versions": "< 2.0", "fixed_version": "2.0",
         "severity": "critical", "description": "old BIOS vulnerable"},
    ]
}


class TestCveExposureAPI:
    async def test_import_and_list_feed(self, cc_client):
        r = await cc_client.post("/api/firmware/cve-feed", json=FEED)
        assert r.status_code == 200
        assert r.json()["imported"] == 2
        r = await cc_client.get("/api/firmware/cve-feed")
        assert len(r.json()["entries"]) == 2

    async def test_import_idempotent(self, cc_client):
        await cc_client.post("/api/firmware/cve-feed", json=FEED)
        await cc_client.post("/api/firmware/cve-feed", json=FEED)
        r = await cc_client.get("/api/firmware/cve-feed")
        assert len(r.json()["entries"]) == 2

    async def test_exposure_matches_only_affected(self, cc_client):
        await cc_client.post("/api/firmware/cve-feed", json=FEED)
        r = await cc_client.get("/api/firmware/exposure")
        assert r.status_code == 200
        data = r.json()
        assert data["devices_scanned"] == 2
        exposures = data["exposures"]
        # agent-old: BMC 7.00 < 7.10.30 AND BIOS 1.15.2 < 2.0 -> 2 hits
        # agent-new: BMC 7.20 and BIOS 2.3.1 are patched -> 0 hits
        assert len(exposures) == 2
        assert {e["agent_id"] for e in exposures} == {"agent-old"}
        assert {e["cve_id"] for e in exposures} == {
            "EXAMPLE-2026-0001", "EXAMPLE-2026-0003",
        }

    async def test_empty_feed_no_exposures(self, cc_client):
        r = await cc_client.get("/api/firmware/exposure")
        assert r.json()["exposures"] == []
        assert r.json()["feed_entries"] == 0

    async def test_malformed_entries_skipped(self, cc_client):
        r = await cc_client.post("/api/firmware/cve-feed", json={
            "entries": [
                {"cve_id": "", "affected_versions": "< 1"},
                {"cve_id": "X-1"},
                {"cve_id": "OK-1", "affected_versions": "< 1.0"},
            ]
        })
        assert r.json()["imported"] == 1
