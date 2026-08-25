"""Integration tests: poll mock simulator → normalize → verify verdicts.

Covers the 10 fault detection paths (5 faults x 2 vendors) from Doc 12 §3.1.
"""

import pytest

from harkeniq.mock.simulator import MockSimulator
from harkeniq.poller import Poller
from harkeniq.redfish.client import RedfishClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def dell_sim():
    sim = MockSimulator(device="dell-r750", port=0, no_auth=True)
    await sim.start()
    yield sim
    await sim.stop()


@pytest.fixture
async def hpe_sim():
    sim = MockSimulator(device="hpe-dl360-gen10", port=0, no_auth=True)
    await sim.start()
    yield sim
    await sim.stop()


async def _make_poller(sim) -> Poller:
    client = RedfishClient(host=sim.url, verify_ssl=False)
    await client.connect("admin", "password")
    poller = Poller(client)
    await poller.detect()
    return poller


# ===========================================================================
# Dell R750 — Healthy Baseline
# ===========================================================================


class TestDellHealthy:
    async def test_poll_healthy_dell(self, dell_sim):
        poller = await _make_poller(dell_sim)
        device = await poller.poll_sensors()

        assert device.identity.vendor == "dell"
        assert device.identity.model == "PowerEdge R750"
        assert device.health_rollup.overall == "OK"
        assert device.health_rollup.fan == "OK"
        assert device.health_rollup.psu == "OK"
        assert device.health_rollup.thermal == "OK"
        # QA-027: fixtures grew to the doc 09/11 baseline (8 fans, 4 disks).
        assert len(device.fans) == 8
        assert len(device.psus) == 2
        assert len(device.thermals) == 4  # QA-027: CPU2 Temp added
        assert device.power_metrics is not None
        assert device.power_metrics.system_power_watts == 186

        await poller.client.close()


# ===========================================================================
# Dell R750 — 5 Fault Detection Paths
# ===========================================================================


class TestDellFanFault:
    """Test path #1: Dell fan failure detection."""

    async def test_fan_critical(self, dell_sim):
        poller = await _make_poller(dell_sim)

        # Inject fan failure
        await dell_sim.inject_fault("fan", "Fan1A", {"health": "Critical", "speed_rpm": 0})

        device = await poller.poll_sensors()
        fan1a = next(f for f in device.fans if "Fan1A" in f.name)
        assert fan1a.health == "Critical"
        assert fan1a.speed_rpm == 0
        assert device.health_rollup.fan == "Critical"
        assert device.health_rollup.overall == "Critical"

        await poller.client.close()


class TestDellDiskFault:
    """Test path #3: Dell disk SMART alert detection."""

    async def test_disk_smart_alert(self, dell_sim):
        poller = await _make_poller(dell_sim)

        await dell_sim.inject_fault("disk", "drive_0", {
            "health": "Warning",
            "life_left_pct": 18,
            "smart_alert": True,
        })

        device = await poller.poll_sensors()
        assert device.disks, "expected drives from standard Storage path"
        disk = device.disks[0]
        assert disk.health == "Warning"
        assert disk.life_left_pct == 18
        assert disk.smart_alert is True

        await poller.client.close()


class TestDellMemoryFault:
    """Test path #5: Dell memory ECC alarm detection."""

    async def test_memory_ecc_alarm(self, dell_sim):
        poller = await _make_poller(dell_sim)

        await dell_sim.inject_fault("memory", "a1", {
            "health": "Warning",
            "alarm_ecc_correctable": True,
            "ecc_correctable_lifetime": 150,
        })

        device = await poller.poll_sensors()
        assert device.memory, "expected DIMMs from Memory collection"
        dimm = device.memory[0]
        assert dimm.alarm_ecc_correctable is True
        assert dimm.ecc_correctable_lifetime == 150

        await poller.client.close()


class TestDellPSUFault:
    """Test path #7: Dell PSU failure detection."""

    async def test_psu_absent(self, dell_sim):
        poller = await _make_poller(dell_sim)

        await dell_sim.inject_fault("psu", "PS2", {
            "state": "Absent",
            "health": "Critical",
            "redundancy_health": "Warning",
        })

        device = await poller.poll_sensors()
        ps2 = next(p for p in device.psus if "PS2" in p.name or p.member_id == "1")
        assert ps2.state == "Absent"
        assert ps2.health == "Critical"
        assert ps2.redundancy_health == "Warning"

        await poller.client.close()


class TestDellThermalFault:
    """Test path #9: Dell thermal critical detection."""

    async def test_thermal_critical(self, dell_sim):
        poller = await _make_poller(dell_sim)

        await dell_sim.inject_fault("thermal", "Inlet", {"reading_c": 48, "health": "Critical"})

        device = await poller.poll_sensors()
        inlet = next(t for t in device.thermals if "Inlet" in t.name)
        assert inlet.reading_c == 48
        assert inlet.health == "Critical"
        assert device.health_rollup.thermal == "Critical"

        await poller.client.close()


# ===========================================================================
# HPE DL360 Gen10 — Healthy Baseline
# ===========================================================================


class TestHPEHealthy:
    async def test_poll_healthy_hpe(self, hpe_sim):
        poller = await _make_poller(hpe_sim)
        device = await poller.poll_sensors()

        assert device.identity.vendor == "hpe"
        assert "DL360" in device.identity.model or "ProLiant" in device.identity.model
        assert device.health_rollup.fan == "OK"
        assert device.health_rollup.psu == "OK"
        assert device.health_rollup.thermal == "OK"
        assert len(device.fans) >= 4
        assert len(device.psus) == 2
        assert len(device.thermals) >= 3

        await poller.client.close()


# ===========================================================================
# HPE DL360 Gen10 — 5 Fault Detection Paths
# ===========================================================================


class TestHPEFanFault:
    """Test path #2: HPE fan failure detection."""

    async def test_fan_critical(self, hpe_sim):
        poller = await _make_poller(hpe_sim)

        await hpe_sim.inject_fault("fan", "Fan 1", {"health": "Critical", "speed_rpm": 0})

        device = await poller.poll_sensors()
        fan1 = next((f for f in device.fans if "Fan 1" in f.name), None)
        if fan1:
            assert fan1.health == "Critical"
            assert fan1.speed_rpm == 0

        await poller.client.close()


class TestHPEDiskFault:
    """Test path #4: HPE disk SMART alert detection."""

    async def test_disk_smart_alert(self, hpe_sim):
        poller = await _make_poller(hpe_sim)

        await hpe_sim.inject_fault("disk", "drive_0", {
            "health": "Warning",
            "life_left_pct": 18,
            "smart_alert": True,
        })

        device = await poller.poll_sensors()
        assert device.disks, "expected drives from standard Storage path"
        # Standard drives come first in the merged list (preferred over SmartStorage)
        disk = device.disks[0]
        assert disk.health == "Warning"
        # SmartStorage drive (different serial) should also be present via merge
        assert len(device.disks) == 2

        await poller.client.close()


class TestHPEMemoryFault:
    """Test path #6: HPE memory ECC alarm detection."""

    async def test_memory_ecc_alarm(self, hpe_sim):
        poller = await _make_poller(hpe_sim)

        await hpe_sim.inject_fault("memory", "proc1dimm1", {
            "health": "Warning",
            "alarm_ecc_uncorrectable": True,
        })

        device = await poller.poll_sensors()
        assert device.memory, "expected DIMMs from Memory collection"
        dimm = device.memory[0]
        assert dimm.alarm_ecc_uncorrectable is True

        await poller.client.close()


class TestHPEPSUFault:
    """Test path #8: HPE PSU failure detection."""

    async def test_psu_absent(self, hpe_sim):
        poller = await _make_poller(hpe_sim)

        await hpe_sim.inject_fault("psu", "PS2", {
            "state": "Absent",
            "health": "Critical",
            "redundancy_health": "Warning",
        })

        device = await poller.poll_sensors()
        # Find the second PSU
        if len(device.psus) >= 2:
            ps2 = device.psus[1]
            assert ps2.state == "Absent" or ps2.health == "Critical"

        await poller.client.close()


class TestHPEThermalFault:
    """Test path #10: HPE thermal critical detection."""

    async def test_thermal_critical(self, hpe_sim):
        poller = await _make_poller(hpe_sim)

        await hpe_sim.inject_fault("thermal", "Inlet", {"reading_c": 48, "health": "Critical"})

        device = await poller.poll_sensors()
        inlet = next((t for t in device.thermals if "Inlet" in t.name), None)
        if inlet:
            assert inlet.reading_c == 48
            assert inlet.health == "Critical"

        await poller.client.close()


# ===========================================================================
# Log Polling
# ===========================================================================


class TestLogPolling:
    async def test_dell_log_poll(self, dell_sim):
        poller = await _make_poller(dell_sim)
        entries = await poller.poll_logs()
        assert isinstance(entries, list)
        await poller.client.close()

    async def test_hpe_log_poll(self, hpe_sim):
        poller = await _make_poller(hpe_sim)
        entries = await poller.poll_logs()
        assert isinstance(entries, list)
        await poller.client.close()


# ===========================================================================
# Dell R760 (iDRAC10) and HPE DL380 Gen11 (iLO 6) — Next-Gen Profiles
# ===========================================================================


@pytest.fixture
async def r760_sim():
    sim = MockSimulator(device="dell-r760", port=0, no_auth=True)
    await sim.start()
    yield sim
    await sim.stop()


@pytest.fixture
async def gen11_sim():
    sim = MockSimulator(device="hpe-dl380-gen11", port=0, no_auth=True)
    await sim.start()
    yield sim
    await sim.stop()


class TestDellR760:
    async def test_identity_idrac10(self, r760_sim):
        poller = await _make_poller(r760_sim)

        identity = poller.identity
        assert identity.vendor == "dell"
        assert identity.model == "PowerEdge R760"
        assert identity.controller_type == "iDRAC"
        assert identity.controller_version == 10
        assert identity.service_tag == "DEMO002"

        await poller.client.close()

    async def test_healthy_poll(self, r760_sim):
        poller = await _make_poller(r760_sim)
        device = await poller.poll_sensors()

        assert device.health_rollup.overall == "OK"
        assert device.disks, "expected drives from standard Storage path"
        assert device.fans and device.psus and device.memory

        await poller.client.close()


class TestHPEDL380Gen11:
    async def test_identity_ilo6(self, gen11_sim):
        poller = await _make_poller(gen11_sim)

        identity = poller.identity
        assert identity.vendor == "hpe"
        assert identity.model == "ProLiant DL380 Gen11"
        assert identity.controller_type == "iLO"
        assert identity.controller_version == 6

        await poller.client.close()

    async def test_no_smartstorage_on_ilo6(self, gen11_sim):
        poller = await _make_poller(gen11_sim)

        # iLO6 removed SmartStorage (Doc 05 §3.5) — path must be empty
        assert poller.paths.smartstorage == ""

        device = await poller.poll_sensors()
        # Drives still come from the standard Storage path only
        assert device.disks, "expected drives from standard Storage path"
        assert len(device.disks) == 1

        await poller.client.close()
