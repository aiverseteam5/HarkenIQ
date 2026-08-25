"""Unit tests for the mock Redfish simulator."""

import ssl

import aiohttp
import pytest

from harkeniq.mock.simulator import MockSimulator


@pytest.fixture
async def sim():
    """Start a Dell R750 mock simulator for testing."""
    simulator = MockSimulator(device="dell-r750", port=0, no_auth=True)
    await simulator.start()
    yield simulator
    await simulator.stop()


@pytest.fixture
def ssl_ctx():
    """SSL context that accepts self-signed certs."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class TestSimulatorLifecycle:
    async def test_start_stop(self):
        sim = MockSimulator(device="dell-r750", port=0, no_auth=True)
        await sim.start()
        assert sim.port > 0
        await sim.stop()

    async def test_invalid_device(self):
        with pytest.raises(ValueError, match="Unknown device profile"):
            MockSimulator(device="invalid-device")


class TestRedfishEndpoints:
    async def test_service_root(self, sim, ssl_ctx):
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{sim.url}/redfish/v1/", ssl=ssl_ctx) as resp:
                assert resp.status == 200
                data = await resp.json()
                assert data["RedfishVersion"] == "1.17.0"
                assert "Dell" in data["Oem"]

    async def test_manager(self, sim, ssl_ctx):
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{sim.url}/redfish/v1/Managers/iDRAC.Embedded.1", ssl=ssl_ctx) as resp:
                assert resp.status == 200
                data = await resp.json()
                assert data["Model"] == "iDRAC9"

    async def test_thermal(self, sim, ssl_ctx):
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{sim.url}/redfish/v1/Chassis/System.Embedded.1/Thermal", ssl=ssl_ctx) as resp:
                assert resp.status == 200
                data = await resp.json()
                assert len(data["Fans"]) == 8  # QA-027: doc 09/11 baseline
                assert data["Fans"][0]["Reading"] == 9800
                assert len(data["Temperatures"]) == 4  # QA-027: CPU2 Temp added
                assert data["Temperatures"][0]["ReadingCelsius"] == 22

    async def test_power(self, sim, ssl_ctx):
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{sim.url}/redfish/v1/Chassis/System.Embedded.1/Power", ssl=ssl_ctx) as resp:
                assert resp.status == 200
                data = await resp.json()
                assert len(data["PowerSupplies"]) == 2
                assert data["PowerControl"][0]["PowerConsumedWatts"] == 186

    async def test_drive(self, sim, ssl_ctx):
        async with aiohttp.ClientSession() as session:
            drive_path = "/redfish/v1/Systems/System.Embedded.1/Storage/RAID.Slot.1-1/Drives/Disk.Bay.0:Enclosure.Internal.0-1:RAID.Slot.1-1"
            async with session.get(f"{sim.url}{drive_path}", ssl=ssl_ctx) as resp:
                assert resp.status == 200
                data = await resp.json()
                assert data["MediaType"] == "SSD"
                assert data["PredictedMediaLifeLeftPercent"] == 98

    async def test_404_unknown_path(self, sim, ssl_ctx):
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{sim.url}/redfish/v1/NoSuchResource", ssl=ssl_ctx) as resp:
                assert resp.status == 404


class TestSessionAuth:
    async def test_auth_required(self, ssl_ctx):
        sim = MockSimulator(device="dell-r750", port=0, no_auth=False)
        await sim.start()
        try:
            async with aiohttp.ClientSession() as session:
                # Without token: 401
                async with session.get(f"{sim.url}/redfish/v1/", ssl=ssl_ctx) as resp:
                    assert resp.status == 401

                # Create session
                async with session.post(
                    f"{sim.url}/redfish/v1/SessionService/Sessions",
                    json={"UserName": "admin", "Password": "password"},
                    ssl=ssl_ctx,
                ) as resp:
                    assert resp.status == 201
                    token = resp.headers["X-Auth-Token"]
                    assert token

                # With token: 200
                async with session.get(
                    f"{sim.url}/redfish/v1/",
                    headers={"X-Auth-Token": token},
                    ssl=ssl_ctx,
                ) as resp:
                    assert resp.status == 200
        finally:
            await sim.stop()

    async def test_invalid_credentials(self, ssl_ctx):
        sim = MockSimulator(device="dell-r750", port=0, no_auth=False)
        await sim.start()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{sim.url}/redfish/v1/SessionService/Sessions",
                    json={"UserName": "admin", "Password": "wrong"},
                    ssl=ssl_ctx,
                ) as resp:
                    assert resp.status == 401
        finally:
            await sim.stop()


class TestFaultInjection:
    async def test_inject_fan_fault(self, sim, ssl_ctx):
        async with aiohttp.ClientSession() as session:
            # Inject fan failure
            async with session.post(
                f"{sim.url}/test/inject-fault",
                json={"fault_type": "fan", "target": "Fan1A", "params": {"health": "Critical", "speed_rpm": 0}},
                ssl=ssl_ctx,
            ) as resp:
                assert resp.status == 200

            # Verify fan is now critical
            async with session.get(f"{sim.url}/redfish/v1/Chassis/System.Embedded.1/Thermal", ssl=ssl_ctx) as resp:
                data = await resp.json()
                fan1a = data["Fans"][0]
                assert fan1a["Status"]["Health"] == "Critical"
                assert fan1a["Reading"] == 0

    async def test_inject_psu_absent(self, sim, ssl_ctx):
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{sim.url}/test/inject-fault",
                json={"fault_type": "psu", "target": "PS2", "params": {"state": "Absent", "redundancy_health": "Warning"}},
                ssl=ssl_ctx,
            ) as resp:
                assert resp.status == 200

            async with session.get(f"{sim.url}/redfish/v1/Chassis/System.Embedded.1/Power", ssl=ssl_ctx) as resp:
                data = await resp.json()
                ps2 = data["PowerSupplies"][1]
                assert ps2["Status"]["State"] == "Absent"
                assert data["Redundancy"][0]["Status"]["Health"] == "Warning"

    async def test_inject_thermal_fault(self, sim, ssl_ctx):
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{sim.url}/test/inject-fault",
                json={"fault_type": "thermal", "target": "Inlet", "params": {"reading_c": 48}},
                ssl=ssl_ctx,
            ) as resp:
                assert resp.status == 200

            async with session.get(f"{sim.url}/redfish/v1/Chassis/System.Embedded.1/Thermal", ssl=ssl_ctx) as resp:
                data = await resp.json()
                inlet = data["Temperatures"][0]
                assert inlet["ReadingCelsius"] == 48

    async def test_reset(self, sim, ssl_ctx):
        async with aiohttp.ClientSession() as session:
            # Inject fault
            await session.post(
                f"{sim.url}/test/inject-fault",
                json={"fault_type": "fan", "target": "Fan1A", "params": {"health": "Critical"}},
                ssl=ssl_ctx,
            )

            # Reset
            async with session.post(f"{sim.url}/test/reset", ssl=ssl_ctx) as resp:
                assert resp.status == 200

            # Verify healthy again
            async with session.get(f"{sim.url}/redfish/v1/Chassis/System.Embedded.1/Thermal", ssl=ssl_ctx) as resp:
                data = await resp.json()
                assert data["Fans"][0]["Status"]["Health"] == "OK"

    async def test_get_state(self, sim, ssl_ctx):
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{sim.url}/test/state", ssl=ssl_ctx) as resp:
                assert resp.status == 200
                state = await resp.json()
                assert "thermal" in state
                assert "power" in state

    async def test_inject_log(self, sim, ssl_ctx):
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{sim.url}/test/inject-log",
                json={"severity": "Critical", "message": "Fan1A failed", "message_id": "FAN0001"},
                ssl=ssl_ctx,
            ) as resp:
                assert resp.status == 200


class TestProgrammaticAPI:
    async def test_inject_fault_programmatic(self, sim, ssl_ctx):
        await sim.inject_fault("fan", "Fan1A", {"health": "Critical", "speed_rpm": 0})

        async with aiohttp.ClientSession() as session:
            async with session.get(f"{sim.url}/redfish/v1/Chassis/System.Embedded.1/Thermal", ssl=ssl_ctx) as resp:
                data = await resp.json()
                assert data["Fans"][0]["Status"]["Health"] == "Critical"

    async def test_reset_programmatic(self, sim, ssl_ctx):
        await sim.inject_fault("fan", "Fan1A", {"health": "Critical"})
        await sim.reset()

        async with aiohttp.ClientSession() as session:
            async with session.get(f"{sim.url}/redfish/v1/Chassis/System.Embedded.1/Thermal", ssl=ssl_ctx) as resp:
                data = await resp.json()
                assert data["Fans"][0]["Status"]["Health"] == "OK"


class TestDimmGeneration:
    """Doc 11 §5: single DIMM template expands to all 16 DIMMs."""

    async def test_dell_16_dimms_generated(self, sim, ssl_ctx):
        coll = sim._state["memory_collection"]
        assert coll["Members@odata.count"] == 16
        ids = [m["@odata.id"] for m in coll["Members"]]
        assert ids[0].endswith("DIMM.Socket.A1")
        assert ids[-1].endswith("DIMM.Socket.B8")

        # Generated DIMM has slot-specific ID, name, bank, serial
        b3 = sim._state["memory_dimm_b3"]
        assert b3["Id"] == "DIMM.Socket.B3"
        assert b3["Name"] == "DIMM B3"
        assert b3["Oem"]["Dell"]["DellMemory"]["BankLabel"] == "B"
        assert b3["SerialNumber"] != sim._state["memory_dimm_a1"]["SerialNumber"]

        # Metrics generated alongside
        assert "memory_metrics_b3" in sim._state

        # Serials unique across all 16
        serials = {
            sim._state[f"memory_dimm_{b}{s}"]["SerialNumber"]
            for b in ("a", "b") for s in range(1, 9)
        }
        assert len(serials) == 16

    async def test_hpe_16_dimms_generated(self):
        from harkeniq.mock.simulator import MockSimulator

        hpe = MockSimulator(device="hpe-dl360-gen10", port=0, no_auth=True)
        await hpe.start()
        try:
            coll = hpe._state["memory_collection"]
            assert coll["Members@odata.count"] == 16

            d = hpe._state["memory_dimm_proc2dimm5"]
            assert d["Id"] == "proc2dimm5"
            assert d["DeviceLocator"] == "PROC 2 DIMM 5"
            assert d["MemoryLocation"]["Socket"] == 2
            assert d["MemoryLocation"]["Slot"] == 5
            assert "memory_metrics_proc2dimm5" in hpe._state
        finally:
            await hpe.stop()

    async def test_generated_dimm_served_over_http(self, sim, ssl_ctx):
        import aiohttp

        async with aiohttp.ClientSession() as session:
            uri = f"{sim.url}/redfish/v1/Systems/System.Embedded.1/Memory/DIMM.Socket.B8"
            async with session.get(uri, ssl=ssl_ctx) as resp:
                assert resp.status == 200
                data = await resp.json()
                assert data["Id"] == "DIMM.Socket.B8"
            async with session.get(f"{uri}/MemoryMetrics", ssl=ssl_ctx) as resp:
                assert resp.status == 200
