"""Mock IPMI BMC -- in-process test double for the IPMI backend seam (R4-1).

Emulates the sync IPMIBackend interface that PyghmiBackend implements
(get_sensors / get_inventory / get_event_log / set_identify / close),
plus fault injection mirroring MockSimulator's semantics. No UDP socket,
no OpenIPMI binary, no pyghmi import -- sensors are plain objects with
the same attribute shape as pyghmi's SensorReading.

Usage in tests:

    bmc = MockIPMIBMC(device="dell-r750")
    proto = IPMIProtocol(host="10.0.0.1", backend_factory=bmc.factory())
    await proto.connect({"username": "admin", "password": "password"})
    bmc.inject_fault("fan_failure", name="Fan1A")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# pyghmi.constants.Health values (mirrored; no pyghmi import in the mock)
HEALTH_OK = 0
HEALTH_WARNING = 1
HEALTH_CRITICAL = 2
HEALTH_FAILED = 4


@dataclass
class FakeSensorReading:
    """Attribute-compatible stand-in for pyghmi.ipmi.sdr.SensorReading."""

    name: str
    type: str
    value: Optional[float] = None
    units: str = ""
    health: int = HEALTH_OK
    states: list[str] = field(default_factory=list)
    unavailable: int = 0
    imprecision: Optional[float] = None
    state_ids: list = field(default_factory=list)


_DEVICE_FRU = {
    "dell-r750": {
        "Manufacturer": "Dell Inc.",
        "Product name": "PowerEdge R750",
        "Serial Number": "IPMI750X",
        "Hardware Version": "A02",
    },
    "hpe-dl380": {
        "Manufacturer": "Hewlett Packard Enterprise",
        "Product name": "ProLiant DL380 Gen11",
        "Serial Number": "IPMI380X",
        "Hardware Version": "B01",
    },
}


class MockIPMIBMC:
    """In-process IPMI BMC double with fault injection."""

    def __init__(
        self,
        device: str = "dell-r750",
        username: str = "admin",
        password: str = "password",
    ) -> None:
        if device not in _DEVICE_FRU:
            raise ValueError(f"Unknown mock device: {device}")
        self.device = device
        self.username = username
        self.password = password
        self.identify_on = False
        self.identify_blink = False
        self.sel_clear_count = 0
        self.closed = False
        self._sel: list[dict] = []
        self._sensors: dict[str, FakeSensorReading] = {}
        self._build_healthy()

    # -- construction -------------------------------------------------------

    def _build_healthy(self) -> None:
        sensors: list[FakeSensorReading] = []
        for i in range(1, 7):
            sensors.append(FakeSensorReading(
                name=f"Fan{i}A", type="Fan", value=8400.0, units="RPM",
            ))
        sensors.extend([
            FakeSensorReading(name="Inlet Temp", type="Temperature",
                              value=22.0, units="°C"),
            FakeSensorReading(name="Exhaust Temp", type="Temperature",
                              value=34.0, units="°C"),
            FakeSensorReading(name="CPU1 Temp", type="Temperature",
                              value=47.0, units="°C"),
            FakeSensorReading(name="CPU2 Temp", type="Temperature",
                              value=45.0, units="°C"),
        ])
        for i in (1, 2):
            sensors.append(FakeSensorReading(
                name=f"PSU{i} Status", type="Power Supply",
                states=["Presence detected"],
            ))
        for slot in ("A1", "A2", "B1", "B2"):
            sensors.append(FakeSensorReading(
                name=f"DIMM {slot}", type="Memory",
                states=["Presence detected"],
            ))
        for i in range(4):
            sensors.append(FakeSensorReading(
                name=f"Drive {i}", type="Drive Bay",
                states=["Drive Present"],
            ))
        sensors.append(FakeSensorReading(
            name="Pwr Consumption", type="Current", value=248.0, units="W",
        ))
        self._sensors = {s.name: s for s in sensors}

    def factory(self):
        """Return a backend_factory for IPMIProtocol.

        Signature matches PyghmiBackend(host, port, username, password);
        raises on bad credentials the way pyghmi raises IpmiException on
        RAKP authentication failure.
        """
        def _factory(host: str, port: int, username: str, password: str):
            if username != self.username or password != self.password:
                raise Exception("Incorrect password provided (RAKP failure)")
            return self
        return _factory

    # -- IPMIBackend interface ----------------------------------------------

    def get_sensors(self) -> list[FakeSensorReading]:
        return list(self._sensors.values())

    def get_inventory(self) -> dict:
        return dict(_DEVICE_FRU[self.device])

    def get_event_log(self, clear: bool = False) -> list[dict]:
        events = list(self._sel)
        if clear:
            self._sel = []
            self.sel_clear_count += 1
        return events

    def set_identify(self, on: bool = True, blink: bool = False) -> None:
        self.identify_on = on
        self.identify_blink = blink

    def close(self) -> None:
        self.closed = True

    # -- fault injection (mirrors MockSimulator semantics) -------------------

    def inject_fault(self, fault: str, name: Optional[str] = None) -> None:
        """Inject a fault into the sensor set.

        Supported faults:
          fan_failure    -- fan reading 0 RPM, health Critical
          fan_degraded   -- fan at reduced RPM, health Warning
          temp_critical  -- temperature spike, health Critical
          psu_failure    -- PSU failure state, health Critical
          psu_absent     -- PSU removed (unavailable)
          disk_fault     -- drive fault state, health Critical
          disk_predictive-- predictive failure state, health Warning
          memory_ecc     -- correctable ECC on a DIMM, health Warning
          memory_ecc_uncorrectable -- uncorrectable ECC, health Critical
        """
        handlers = {
            "fan_failure": (name or "Fan1A", self._fan_failure),
            "fan_degraded": (name or "Fan1A", self._fan_degraded),
            "temp_critical": (name or "CPU1 Temp", self._temp_critical),
            "psu_failure": (name or "PSU1 Status", self._psu_failure),
            "psu_absent": (name or "PSU1 Status", self._psu_absent),
            "disk_fault": (name or "Drive 0", self._disk_fault),
            "disk_predictive": (name or "Drive 0", self._disk_predictive),
            "memory_ecc": (name or "DIMM A1", self._memory_ecc),
            "memory_ecc_uncorrectable": (name or "DIMM A1",
                                         self._memory_ecc_uncorrectable),
        }
        if fault not in handlers:
            raise ValueError(f"Unknown fault: {fault}")
        sensor_name, handler = handlers[fault]
        sensor = self._sensors.get(sensor_name)
        if sensor is None:
            raise ValueError(f"Unknown sensor: {sensor_name}")
        handler(sensor)

    def add_sel_event(
        self,
        message: str,
        severity: int = HEALTH_CRITICAL,
        component: str = "",
        component_type: str = "",
    ) -> None:
        self._sel.append({
            "record_id": len(self._sel) + 1,
            "timestamp": "2026-08-24T12:00:00",
            "severity": severity,
            "event": message,
            "component": component,
            "component_type": component_type,
        })

    def reset(self) -> None:
        """Restore the healthy baseline (keeps identify/SEL counters)."""
        self._build_healthy()
        self._sel = []

    # -- fault handlers ------------------------------------------------------

    def _fan_failure(self, s: FakeSensorReading) -> None:
        s.value = 0.0
        s.health = HEALTH_CRITICAL
        self.add_sel_event(f"{s.name} fan failure", HEALTH_CRITICAL,
                           component=s.name, component_type="Fan")

    def _fan_degraded(self, s: FakeSensorReading) -> None:
        s.value = 2100.0
        s.health = HEALTH_WARNING

    def _temp_critical(self, s: FakeSensorReading) -> None:
        s.value = 98.0
        s.health = HEALTH_CRITICAL
        self.add_sel_event(f"{s.name} upper critical threshold crossed",
                           HEALTH_CRITICAL, component=s.name,
                           component_type="Temperature")

    def _psu_failure(self, s: FakeSensorReading) -> None:
        s.health = HEALTH_CRITICAL
        s.states = ["Presence detected", "Power supply failure detected"]
        self.add_sel_event(f"{s.name} power supply failure", HEALTH_CRITICAL,
                           component=s.name, component_type="Power Supply")

    def _psu_absent(self, s: FakeSensorReading) -> None:
        s.unavailable = 1
        s.states = []

    def _disk_fault(self, s: FakeSensorReading) -> None:
        s.health = HEALTH_CRITICAL
        s.states = ["Drive Present", "Drive Fault"]

    def _disk_predictive(self, s: FakeSensorReading) -> None:
        s.health = HEALTH_WARNING
        s.states = ["Drive Present", "Predictive Failure"]

    def _memory_ecc(self, s: FakeSensorReading) -> None:
        s.health = HEALTH_WARNING
        s.states = ["Presence detected", "Correctable ECC"]

    def _memory_ecc_uncorrectable(self, s: FakeSensorReading) -> None:
        s.health = HEALTH_CRITICAL
        s.states = ["Presence detected", "Uncorrectable ECC"]
