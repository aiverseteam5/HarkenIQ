"""Unit tests for the Redfish HTTP client against the mock simulator."""

import pytest

from harkeniq.errors import RedfishAuthError, RedfishConnectionError, RedfishResponseError, RedfishTimeoutError
from harkeniq.mock.simulator import MockSimulator
from harkeniq.redfish.client import RedfishClient


@pytest.fixture
async def sim():
    """Start a Dell R750 mock simulator."""
    simulator = MockSimulator(device="dell-r750", port=0, no_auth=True)
    await simulator.start()
    yield simulator
    await simulator.stop()


@pytest.fixture
async def sim_auth():
    """Start a mock simulator with auth enabled."""
    simulator = MockSimulator(device="dell-r750", port=0, no_auth=False)
    await simulator.start()
    yield simulator
    await simulator.stop()


@pytest.fixture
async def client(sim):
    """Create a connected RedfishClient against the mock."""
    c = RedfishClient(host=sim.url, verify_ssl=False, request_timeout=10)
    await c.connect("admin", "password")
    yield c
    await c.close()


class TestConnection:
    async def test_connect_no_auth(self, sim):
        c = RedfishClient(host=sim.url, verify_ssl=False)
        await c.connect("admin", "password")
        assert c._token is not None or sim.no_auth
        await c.close()

    async def test_connect_with_auth(self, sim_auth):
        c = RedfishClient(host=sim_auth.url, verify_ssl=False)
        await c.connect("admin", "password")
        assert c._token
        await c.close()

    async def test_connect_wrong_password(self, sim_auth):
        c = RedfishClient(host=sim_auth.url, verify_ssl=False)
        with pytest.raises(RedfishAuthError):
            await c.connect("admin", "wrong")
        await c.close()

    async def test_connect_unreachable(self):
        c = RedfishClient(host="https://127.0.0.1:19999", verify_ssl=False, request_timeout=2)
        with pytest.raises((RedfishConnectionError, RedfishTimeoutError)):
            await c.connect("admin", "password")
        await c.close()


class TestGet:
    async def test_get_service_root(self, client):
        data = await client.get("/redfish/v1/")
        assert data["RedfishVersion"] == "1.17.0"
        assert "Dell" in data["Oem"]

    async def test_get_thermal(self, client):
        data = await client.get("/redfish/v1/Chassis/System.Embedded.1/Thermal")
        assert len(data["Fans"]) == 4
        assert data["Fans"][0]["Reading"] == 9800

    async def test_get_power(self, client):
        data = await client.get("/redfish/v1/Chassis/System.Embedded.1/Power")
        assert len(data["PowerSupplies"]) == 2
        assert data["PowerControl"][0]["PowerConsumedWatts"] == 186

    async def test_get_drive(self, client):
        path = "/redfish/v1/Systems/System.Embedded.1/Storage/RAID.Slot.1-1/Drives/Disk.Bay.0:Enclosure.Internal.0-1:RAID.Slot.1-1"
        data = await client.get(path)
        assert data["MediaType"] == "SSD"
        assert data["PredictedMediaLifeLeftPercent"] == 98

    async def test_get_manager(self, client):
        data = await client.get("/redfish/v1/Managers/iDRAC.Embedded.1")
        assert data["Model"] == "iDRAC9"

    async def test_get_404(self, client):
        with pytest.raises(RedfishResponseError) as exc_info:
            await client.get("/redfish/v1/NoSuchResource")
        assert exc_info.value.status_code == 404


class TestSessionRenewal:
    async def test_reauth_on_401(self, sim_auth):
        """Client should re-authenticate transparently when token expires."""
        c = RedfishClient(host=sim_auth.url, verify_ssl=False)
        await c.connect("admin", "password")

        # Invalidate the token by clearing server-side sessions
        sim_auth._sessions.clear()

        # GET should trigger re-auth and succeed
        data = await c.get("/redfish/v1/")
        assert data["RedfishVersion"] == "1.17.0"
        await c.close()


class TestDeleteSession:
    async def test_delete_session(self, sim_auth):
        c = RedfishClient(host=sim_auth.url, verify_ssl=False)
        await c.connect("admin", "password")
        assert c._token

        await c.delete_session()
        assert c._token is None
        await c.close()


class TestAfterFaultInjection:
    async def test_see_fault_through_client(self, sim, client):
        """Client sees fault state after injection via simulator."""
        await sim.inject_fault("fan", "Fan1A", {"health": "Critical", "speed_rpm": 0})

        data = await client.get("/redfish/v1/Chassis/System.Embedded.1/Thermal")
        fan1a = data["Fans"][0]
        assert fan1a["Status"]["Health"] == "Critical"
        assert fan1a["Reading"] == 0

    async def test_see_healthy_after_reset(self, sim, client):
        await sim.inject_fault("fan", "Fan1A", {"health": "Critical"})
        await sim.reset()

        data = await client.get("/redfish/v1/Chassis/System.Embedded.1/Thermal")
        assert data["Fans"][0]["Status"]["Health"] == "OK"
