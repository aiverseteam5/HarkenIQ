"""IPMIProtocol tests (R4-1 P8).

Covers: pyghmi health mapping, FRU identity detection, sensor
normalization to NormalizedDevice, SEL normalization, fault injection
parity, action execution (IDENTIFY_LED / SEL_CLEAR), executor protocol
dispatch, and the R4-1 exit criterion: the shipped skill files evaluate
IPMI-normalized data identically to Redfish-normalized data.
"""

from __future__ import annotations

import pytest

from harkeniq.actions.executor import ActionExecutor
from harkeniq.actions.queue import ActionQueue
from harkeniq.mock.ipmi_sim import (
    HEALTH_CRITICAL,
    HEALTH_OK,
    HEALTH_WARNING,
    MockIPMIBMC,
)
from harkeniq.models import ActionType, VerdictSeverity
from harkeniq.protocols.device import (
    DeviceProtocol,
    ProtocolError,
    create_device_protocol,
)
from harkeniq.protocols.ipmi import (
    IPMIProtocol,
    health_to_str,
    vendor_from_manufacturer,
)
from harkeniq.skills.engine import SkillEngine
from harkeniq.skills.loader import load_skills

PERMISSIVE_DEBOUNCE = {"critical": [1, 1], "warning": [1, 1], "recovery": [1, 1]}


@pytest.fixture
def bmc():
    return MockIPMIBMC(device="dell-r750")


@pytest.fixture
async def proto(bmc):
    p = IPMIProtocol(host="10.0.0.1", backend_factory=bmc.factory())
    await p.connect({"username": "admin", "password": "password"})
    yield p
    await p.disconnect()


class TestHealthMapping:
    def test_ok(self):
        assert health_to_str(HEALTH_OK) == "OK"

    def test_warning(self):
        assert health_to_str(HEALTH_WARNING) == "Warning"

    def test_critical(self):
        assert health_to_str(HEALTH_CRITICAL) == "Critical"

    def test_failed_maps_to_critical(self):
        assert health_to_str(4) == "Critical"

    def test_combined_critical_wins(self):
        assert health_to_str(HEALTH_WARNING | HEALTH_CRITICAL) == "Critical"

    def test_none_is_unknown(self):
        assert health_to_str(None) == "Unknown"

    def test_unavailable_is_unknown(self):
        assert health_to_str(HEALTH_OK, unavailable=True) == "Unknown"

    def test_only_canonical_strings(self):
        for h in (None, 0, 1, 2, 3, 4, 5, 6, 7):
            assert health_to_str(h) in ("OK", "Warning", "Critical", "Unknown")


class TestVendorMapping:
    def test_dell(self):
        assert vendor_from_manufacturer("Dell Inc.") == "dell"

    def test_hpe(self):
        assert vendor_from_manufacturer("Hewlett Packard Enterprise") == "hpe"
        assert vendor_from_manufacturer("HPE") == "hpe"

    def test_other_vendor_first_token(self):
        assert vendor_from_manufacturer("Supermicro Computer") == "supermicro"

    def test_empty_is_unknown(self):
        assert vendor_from_manufacturer("") == "unknown"


class TestFactory:
    def test_create_ipmi(self):
        p = create_device_protocol("ipmi", host="10.0.0.1")
        assert isinstance(p, IPMIProtocol)
        assert isinstance(p, DeviceProtocol)
        assert p.name == "ipmi"

    def test_default_port_623(self):
        p = create_device_protocol("ipmi", host="10.0.0.1")
        assert p._port == 623

    def test_unknown_still_raises(self):
        with pytest.raises(ValueError):
            create_device_protocol("gnmi", host="x")


class TestConnection:
    async def test_auth_failure_raises_connection_error(self, bmc):
        p = IPMIProtocol(host="10.0.0.1", backend_factory=bmc.factory())
        with pytest.raises(ConnectionError):
            await p.connect({"username": "admin", "password": "wrong"})

    async def test_timeout_maps_to_timeout_error(self):
        def _factory(host, port, username, password):
            raise Exception("timeout waiting for RMCP response")

        p = IPMIProtocol(host="10.0.0.1", backend_factory=_factory)
        with pytest.raises(TimeoutError):
            await p.connect({"username": "a", "password": "b"})

    async def test_not_connected_raises(self):
        p = IPMIProtocol(host="10.0.0.1")
        with pytest.raises(ProtocolError):
            await p.poll_sensors()
        with pytest.raises(ProtocolError):
            await p.detect_identity()
        with pytest.raises(ProtocolError):
            await p.execute_action("IDENTIFY_LED", {})

    async def test_disconnect_closes_backend(self, bmc):
        p = IPMIProtocol(host="10.0.0.1", backend_factory=bmc.factory())
        await p.connect({"username": "admin", "password": "password"})
        await p.disconnect()
        assert bmc.closed is True


class TestIdentity:
    async def test_dell_fru(self, proto):
        identity = await proto.detect_identity()
        assert identity.vendor == "dell"
        assert identity.model == "PowerEdge R750"
        assert identity.service_tag == "IPMI750X"
        assert identity.controller_type == "ipmi"

    async def test_hpe_fru(self):
        bmc = MockIPMIBMC(device="hpe-dl380")
        p = IPMIProtocol(host="10.0.0.2", backend_factory=bmc.factory())
        await p.connect({"username": "admin", "password": "password"})
        identity = await p.detect_identity()
        assert identity.vendor == "hpe"
        assert identity.model == "ProLiant DL380 Gen11"


class TestPollSensors:
    async def test_healthy_device(self, proto):
        device = await proto.poll_sensors()
        assert len(device.fans) == 6
        assert len(device.thermals) == 4
        assert len(device.psus) == 2
        assert len(device.memory) == 4
        assert len(device.disks) == 4
        assert device.power_metrics is not None
        assert device.power_metrics.system_power_watts == 248
        rollup = device.health_rollup
        assert (rollup.fan, rollup.disk, rollup.memory, rollup.psu,
                rollup.thermal, rollup.overall) == ("OK",) * 6

    async def test_fan_fields(self, proto):
        device = await proto.poll_sensors()
        fan = device.fans[0]
        assert fan.name == "Fan1A"
        assert fan.speed_rpm == 8400
        assert fan.health == "OK"
        assert fan.state == "Enabled"

    async def test_thermal_fields(self, proto):
        device = await proto.poll_sensors()
        inlet = next(t for t in device.thermals if t.name == "Inlet Temp")
        assert inlet.reading_c == 22.0
        assert inlet.context == "Intake"
        cpu = next(t for t in device.thermals if t.name == "CPU1 Temp")
        assert cpu.context == "CPU"

    async def test_fan_failure(self, bmc, proto):
        bmc.inject_fault("fan_failure", name="Fan2A")
        device = await proto.poll_sensors()
        fan = next(f for f in device.fans if f.name == "Fan2A")
        assert fan.health == "Critical"
        assert fan.speed_rpm == 0
        assert device.health_rollup.fan == "Critical"
        assert device.health_rollup.overall == "Critical"

    async def test_psu_failure(self, bmc, proto):
        bmc.inject_fault("psu_failure")
        device = await proto.poll_sensors()
        psu = next(p for p in device.psus if p.name == "PSU1 Status")
        assert psu.health == "Critical"
        assert device.health_rollup.psu == "Critical"

    async def test_psu_absent(self, bmc, proto):
        bmc.inject_fault("psu_absent")
        device = await proto.poll_sensors()
        psu = next(p for p in device.psus if p.name == "PSU1 Status")
        assert psu.state == "Absent"

    async def test_disk_fault_sets_smart_alert(self, bmc, proto):
        bmc.inject_fault("disk_predictive", name="Drive 1")
        device = await proto.poll_sensors()
        disk = next(d for d in device.disks if d.name == "Drive 1")
        assert disk.smart_alert is True
        assert disk.health == "Warning"

    async def test_memory_ecc_alarms(self, bmc, proto):
        bmc.inject_fault("memory_ecc", name="DIMM A2")
        device = await proto.poll_sensors()
        dimm = next(m for m in device.memory if m.name == "DIMM A2")
        assert dimm.alarm_ecc_correctable is True
        assert dimm.alarm_ecc_uncorrectable is False

    async def test_memory_uncorrectable_is_critical(self, bmc, proto):
        bmc.inject_fault("memory_ecc_uncorrectable")
        device = await proto.poll_sensors()
        dimm = next(m for m in device.memory if m.name == "DIMM A1")
        assert dimm.health == "Critical"
        assert dimm.alarm_ecc_uncorrectable is True
        assert device.health_rollup.memory == "Critical"

    async def test_sel_entries_normalized(self, bmc, proto):
        bmc.add_sel_event("Fan1A fan failure", HEALTH_CRITICAL,
                          component="Fan1A", component_type="Fan")
        device = await proto.poll_sensors()
        assert len(device.log_entries) == 1
        entry = device.log_entries[0]
        assert entry.severity == "Critical"
        assert entry.message == "Fan1A fan failure"
        assert entry.component_id == "Fan1A"
        assert entry.category == "Fan"

    async def test_sel_read_does_not_clear(self, bmc, proto):
        bmc.add_sel_event("event")
        await proto.poll_sensors()
        await proto.poll_sensors()
        assert bmc.sel_clear_count == 0
        assert len(bmc.get_event_log()) == 1


class TestActions:
    async def test_identify_led(self, bmc, proto):
        result = await proto.execute_action("IDENTIFY_LED", {"target": "Drive 0"})
        assert result["success"] is True
        assert bmc.identify_on is True
        assert bmc.identify_blink is True

    async def test_sel_clear(self, bmc, proto):
        bmc.add_sel_event("old event")
        result = await proto.execute_action("SEL_CLEAR", {})
        assert result["success"] is True
        assert bmc.sel_clear_count == 1
        assert bmc.get_event_log() == []

    async def test_unsupported_action(self, proto):
        result = await proto.execute_action("FAN_RESET", {})
        assert result["success"] is False
        assert "not supported" in result["error"]

    async def test_backend_error_returns_failure(self, bmc, proto):
        def _boom(on=True, blink=False):
            raise Exception("BMC busy")

        bmc.set_identify = _boom
        result = await proto.execute_action("IDENTIFY_LED", {})
        assert result["success"] is False
        assert "BMC busy" in result["error"]


class TestExecutorProtocolDispatch:
    """Agent-level ActionExecutor dispatching through a non-Redfish protocol."""

    def _make_action(self, action_type: ActionType, params=None):
        queue = ActionQueue()
        action = queue.enqueue(
            action_type, "fan:Fan1A", "fan-health",
            VerdictSeverity.CRITICAL, params or {},
        )
        queue.approve(action.id)
        return action

    async def test_identify_led_via_protocol(self, bmc, proto):
        executor = ActionExecutor(
            None, "supermicro",
            {"actions": {"allow_list": ["IDENTIFY_LED"]}},
            protocol=proto,
        )
        action = self._make_action(ActionType.IDENTIFY_LED, {"target": "Drive 0"})
        outcome = await executor.execute(action)
        assert outcome.success is True
        assert bmc.identify_on is True

    async def test_allow_list_still_enforced(self, bmc, proto):
        executor = ActionExecutor(
            None, "supermicro",
            {"actions": {"allow_list": ["IDENTIFY_LED"]}},
            protocol=proto,
        )
        action = self._make_action(ActionType.SEL_CLEAR)
        outcome = await executor.execute(action)
        assert outcome.success is False
        assert "not in allow list" in outcome.error_message
        assert bmc.sel_clear_count == 0

    async def test_sel_clear_via_protocol(self, bmc, proto):
        executor = ActionExecutor(
            None, "supermicro",
            {"actions": {"allow_list": ["SEL_CLEAR"]}},
            protocol=proto,
        )
        bmc.add_sel_event("stale")
        action = self._make_action(ActionType.SEL_CLEAR)
        outcome = await executor.execute(action)
        assert outcome.success is True
        assert bmc.sel_clear_count == 1

    async def test_unsupported_action_is_failed_outcome(self, bmc, proto):
        executor = ActionExecutor(
            None, "supermicro",
            {"actions": {"allow_list": ["FAN_RESET"]}},
            protocol=proto,
        )
        action = self._make_action(ActionType.FAN_RESET)
        outcome = await executor.execute(action)
        assert outcome.success is False
        assert "not supported" in outcome.error_message

    async def test_arbitrary_vendor_accepted_with_protocol(self, proto):
        # IPMI FRU vendors are not limited to dell/hpe; the Redfish vendor
        # check must not apply on the protocol-dispatch path.
        executor = ActionExecutor(None, "quanta", None, protocol=proto)
        assert executor.vendor == "quanta"


class TestSkillParity:
    """R4-1 exit criterion: the shipped skill files evaluate IPMI-normalized
    data exactly as they evaluate Redfish data (same fields, same verdicts)."""

    def _engine(self):
        skills = load_skills("skills")
        return SkillEngine(list(skills.values()), PERMISSIVE_DEBOUNCE)

    async def test_healthy_device_all_healthy(self, proto):
        device = await proto.poll_sensors()
        verdicts = await self._engine().evaluate(device)
        assert verdicts, "skills produced no verdicts on IPMI data"
        assert all(v.severity == VerdictSeverity.HEALTHY for v in verdicts)

    async def test_fan_failure_critical_verdict(self, bmc, proto):
        bmc.inject_fault("fan_failure", name="Fan1A")
        device = await proto.poll_sensors()
        verdicts = await self._engine().evaluate(device)
        fan_verdicts = [v for v in verdicts if v.sensor_id == "fan:Fan1A"]
        assert fan_verdicts
        assert fan_verdicts[0].severity == VerdictSeverity.CRITICAL

    async def test_psu_failure_critical_verdict(self, bmc, proto):
        bmc.inject_fault("psu_failure")
        device = await proto.poll_sensors()
        verdicts = await self._engine().evaluate(device)
        psu = [v for v in verdicts if v.sensor_id == "psu:PSU1 Status"]
        assert psu and psu[0].severity == VerdictSeverity.CRITICAL

    async def test_disk_predictive_warning_verdict(self, bmc, proto):
        bmc.inject_fault("disk_predictive", name="Drive 2")
        device = await proto.poll_sensors()
        verdicts = await self._engine().evaluate(device)
        disk = [v for v in verdicts if v.sensor_id == "disk:Drive 2"]
        assert disk and disk[0].severity == VerdictSeverity.WARNING

    async def test_memory_uncorrectable_critical_verdict(self, bmc, proto):
        bmc.inject_fault("memory_ecc_uncorrectable", name="DIMM B1")
        device = await proto.poll_sensors()
        verdicts = await self._engine().evaluate(device)
        mem = [v for v in verdicts if v.sensor_id == "memory:DIMM B1"]
        assert mem and mem[0].severity == VerdictSeverity.CRITICAL

    async def test_thermal_critical_verdict(self, bmc, proto):
        bmc.inject_fault("temp_critical", name="CPU2 Temp")
        device = await proto.poll_sensors()
        verdicts = await self._engine().evaluate(device)
        therm = [v for v in verdicts if v.sensor_id == "thermal:CPU2 Temp"]
        assert therm and therm[0].severity == VerdictSeverity.CRITICAL
