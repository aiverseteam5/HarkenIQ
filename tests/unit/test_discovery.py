"""Unit tests for BMC vendor detection and identity resolution."""

import pytest

from harkeniq.mock.simulator import MockSimulator
from harkeniq.redfish.client import RedfishClient
from harkeniq.redfish.discovery import (
    RedfishPaths,
    detect_controller,
    detect_identity,
    detect_vendor,
)


@pytest.fixture
async def dell_sim():
    sim = MockSimulator(device="dell-r750", port=0, no_auth=True)
    await sim.start()
    yield sim
    await sim.stop()


@pytest.fixture
async def dell_client(dell_sim):
    c = RedfishClient(host=dell_sim.url, verify_ssl=False)
    await c.connect("admin", "password")
    yield c
    await c.close()


class TestDetectVendor:
    async def test_dell_vendor(self):
        root = {"Oem": {"Dell": {"ServiceTag": "X"}}}
        vendor = await detect_vendor(root)
        assert vendor == "dell"

    async def test_hpe_vendor(self):
        root = {"Oem": {"Hpe": {}}}
        vendor = await detect_vendor(root)
        assert vendor == "hpe"

    async def test_unknown_vendor(self):
        with pytest.raises(ValueError, match="Unsupported vendor"):
            await detect_vendor({"Oem": {"Lenovo": {}}})

    async def test_no_oem(self):
        with pytest.raises(ValueError):
            await detect_vendor({})


class TestDetectController:
    async def test_idrac9(self):
        ct, cv = await detect_controller("dell", {"Model": "iDRAC9"})
        assert ct == "iDRAC"
        assert cv == "9"

    async def test_idrac10(self):
        ct, cv = await detect_controller("dell", {"Model": "iDRAC10"})
        assert ct == "iDRAC"
        assert cv == "10"

    async def test_ilo5(self):
        ct, cv = await detect_controller("hpe", {"Model": "iLO 5"})
        assert ct == "iLO"
        assert cv == "5"

    async def test_ilo6(self):
        ct, cv = await detect_controller("hpe", {"Model": "iLO 6"})
        assert ct == "iLO"
        assert cv == "6"


class TestDetectIdentityDell:
    async def test_full_detection_dell(self, dell_client):
        identity = await detect_identity(dell_client)
        assert identity.vendor == "dell"
        assert identity.model == "PowerEdge R750"
        assert identity.controller_type == "iDRAC"
        assert identity.controller_version == "9"
        assert identity.service_tag == "DEMO001"
        assert identity.firmware_version == "7.00.00.00"
        assert identity.system_id == "System.Embedded.1"
        assert identity.chassis_id == "System.Embedded.1"
        assert identity.manager_id == "iDRAC.Embedded.1"


@pytest.fixture
async def hpe_sim():
    sim = MockSimulator(device="hpe-dl360-gen10", port=0, no_auth=True)
    await sim.start()
    yield sim
    await sim.stop()


@pytest.fixture
async def hpe_client(hpe_sim):
    c = RedfishClient(host=hpe_sim.url, verify_ssl=False)
    await c.connect("admin", "password")
    yield c
    await c.close()


class TestDetectIdentityHPE:
    async def test_full_detection_hpe(self, hpe_client):
        identity = await detect_identity(hpe_client)
        assert identity.vendor == "hpe"
        assert "DL360" in identity.model or "ProLiant" in identity.model
        assert identity.controller_type == "iLO"
        assert identity.controller_version == "5"
        assert identity.system_id == "1"
        assert identity.chassis_id == "1"
        assert identity.manager_id == "1"


class TestRedfishPaths:
    def test_dell_paths(self):
        from harkeniq.redfish.normalize import DeviceIdentity

        identity = DeviceIdentity(
            vendor="dell",
            system_id="System.Embedded.1",
            chassis_id="System.Embedded.1",
            manager_id="iDRAC.Embedded.1",
        )
        paths = RedfishPaths.from_identity(identity)
        assert paths.thermal == "/redfish/v1/Chassis/System.Embedded.1/Thermal"
        assert paths.power == "/redfish/v1/Chassis/System.Embedded.1/Power"
        assert paths.storage == "/redfish/v1/Systems/System.Embedded.1/Storage"
        assert paths.memory == "/redfish/v1/Systems/System.Embedded.1/Memory"
        assert paths.sel_entries == "/redfish/v1/Managers/iDRAC.Embedded.1/LogServices/Sel/Entries"

    def test_hpe_paths(self):
        from harkeniq.redfish.normalize import DeviceIdentity

        identity = DeviceIdentity(
            vendor="hpe",
            system_id="1",
            chassis_id="1",
            manager_id="1",
        )
        paths = RedfishPaths.from_identity(identity)
        assert paths.thermal == "/redfish/v1/Chassis/1/Thermal"
        assert paths.power == "/redfish/v1/Chassis/1/Power"
        assert paths.storage == "/redfish/v1/Systems/1/Storage"
        assert paths.memory == "/redfish/v1/Systems/1/Memory"
        assert paths.sel_entries == "/redfish/v1/Systems/1/LogServices/IML/Entries"
