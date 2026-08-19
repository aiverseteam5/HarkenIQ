"""Vendor normalization layer: normalized sensor data models and dispatcher.

Transforms raw Redfish JSON into vendor-agnostic dataclasses that skills
evaluate against. Skills never see vendor-specific field names.

See Doc 08 for field mapping tables and Doc 10 §1.3 for import contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Device Identity (Doc 08 §3.1)
# ---------------------------------------------------------------------------


@dataclass
class DeviceIdentity:
    """Vendor, model, and controller information detected at startup."""

    vendor: str = ""  # "dell" | "hpe"
    model: str = ""  # e.g. "PowerEdge R750"
    controller_type: str = ""  # "iDRAC" | "iLO"
    controller_version: Optional[int] = None  # 9 | 10 (Dell), 5 | 6 (HPE); None = undetected
    firmware_version: str = ""
    service_tag: str = ""
    system_id: str = ""  # Dell: "System.Embedded.1", HPE: "1"
    chassis_id: str = ""  # Dell: "System.Embedded.1", HPE: "1"
    manager_id: str = ""  # Dell: "iDRAC.Embedded.1", HPE: "1"


# ---------------------------------------------------------------------------
# Normalized Sensor Models (Doc 08 §3.2-3.9)
# ---------------------------------------------------------------------------


@dataclass
class NormalizedFan:
    """Normalized fan sensor data."""

    name: str = ""
    speed_rpm: Optional[int] = None
    speed_pct: Optional[int] = None  # Dell FanPWM; None on HPE
    health: str = "Unknown"
    state: str = "Unknown"
    threshold_low_critical: Optional[int] = None
    redundancy_health: Optional[str] = None
    location: str = ""
    oem_data: dict[str, Any] = field(default_factory=dict)


@dataclass
class NormalizedDisk:
    """Normalized disk/drive sensor data."""

    name: str = ""
    serial: str = ""
    media_type: str = ""  # SSD | HDD
    protocol: str = ""  # SATA | SAS | NVMe
    capacity_bytes: Optional[int] = None
    health: str = "Unknown"
    life_left_pct: Optional[int] = None  # SSD wear (0-100)
    smart_alert: bool = False
    raid_status: Optional[str] = None  # Dell only
    temperature_c: Optional[float] = None  # HPE only
    slot: Optional[int] = None
    oem_data: dict[str, Any] = field(default_factory=dict)


@dataclass
class NormalizedMemory:
    """Normalized DIMM sensor data."""

    name: str = ""
    capacity_mib: Optional[int] = None
    type: str = ""  # DDR4 | DDR5
    speed_mhz: Optional[int] = None
    health: str = "Unknown"
    state: str = "Unknown"
    socket: Optional[int] = None
    channel: Optional[int] = None
    slot: Optional[int] = None
    # None = MemoryMetrics endpoint unavailable (Doc 08 §3.5); 0 = zero errors
    ecc_correctable_lifetime: Optional[int] = None
    ecc_uncorrectable_lifetime: Optional[int] = None
    ecc_correctable_current: Optional[int] = None
    alarm_ecc_correctable: bool = False
    alarm_ecc_uncorrectable: bool = False
    alarm_temperature: bool = False
    oem_data: dict[str, Any] = field(default_factory=dict)


@dataclass
class NormalizedPSU:
    """Normalized power supply sensor data."""

    name: str = ""
    member_id: str = ""
    type: str = ""  # AC | DC
    capacity_watts: Optional[int] = None
    output_watts: Optional[int] = None
    input_voltage: Optional[int] = None
    health: str = "Unknown"
    state: str = "Unknown"
    model: str = ""
    serial: str = ""
    redundancy_health: Optional[str] = None
    redundancy_mode: Optional[str] = None
    oem_data: dict[str, Any] = field(default_factory=dict)


@dataclass
class NormalizedThermal:
    """Normalized temperature sensor data."""

    name: str = ""
    reading_c: Optional[float] = None
    health: str = "Unknown"
    threshold_warning: Optional[float] = None
    threshold_critical: Optional[float] = None
    threshold_fatal: Optional[float] = None
    threshold_cold_warning: Optional[float] = None
    threshold_cold_critical: Optional[float] = None
    context: str = ""  # PhysicalContext
    oem_data: dict[str, Any] = field(default_factory=dict)


@dataclass
class NormalizedPowerMetrics:
    """System-level power metrics."""

    system_power_watts: Optional[int] = None
    system_power_avg: Optional[int] = None
    system_power_peak: Optional[int] = None
    system_power_min: Optional[int] = None
    interval_minutes: Optional[int] = None


@dataclass
class NormalizedLogEntry:
    """Normalized event log entry."""

    id: str = ""
    timestamp: str = ""
    severity: str = ""
    message: str = ""
    message_id: str = ""
    component_id: Optional[str] = None
    category: str = ""


@dataclass
class HealthRollup:
    """Per-subsystem health summary."""

    fan: str = "Unknown"
    disk: str = "Unknown"
    memory: str = "Unknown"
    psu: str = "Unknown"
    thermal: str = "Unknown"
    overall: str = "Unknown"


@dataclass
class NormalizedDevice:
    """Top-level container with identity + all sensor collections."""

    identity: DeviceIdentity = field(default_factory=DeviceIdentity)
    fans: list[NormalizedFan] = field(default_factory=list)
    disks: list[NormalizedDisk] = field(default_factory=list)
    memory: list[NormalizedMemory] = field(default_factory=list)
    psus: list[NormalizedPSU] = field(default_factory=list)
    thermals: list[NormalizedThermal] = field(default_factory=list)
    power_metrics: Optional[NormalizedPowerMetrics] = None
    log_entries: list[NormalizedLogEntry] = field(default_factory=list)
    health_rollup: HealthRollup = field(default_factory=HealthRollup)


# ---------------------------------------------------------------------------
# Health rollup computation (Doc 08 §4.8)
# ---------------------------------------------------------------------------

_HEALTH_ORDER = {"Unknown": 0, "OK": 1, "Warning": 2, "Critical": 3}


def worst_health(healths: list[str]) -> str:
    """Return the worst health status from a list. Empty list returns 'Unknown'."""
    if not healths:
        return "Unknown"
    return max(healths, key=lambda h: _HEALTH_ORDER.get(h, 3))


def compute_health_rollup(device: NormalizedDevice) -> HealthRollup:
    """Compute per-subsystem health rollup from normalized collections."""
    fan_health = worst_health([f.health for f in device.fans])
    disk_health = worst_health([d.health for d in device.disks])
    mem_health = worst_health([m.health for m in device.memory if m.state != "Absent"])
    psu_health = worst_health([p.health for p in device.psus])
    thermal_health = worst_health([t.health for t in device.thermals])
    overall = worst_health([fan_health, disk_health, mem_health, psu_health, thermal_health])
    return HealthRollup(
        fan=fan_health,
        disk=disk_health,
        memory=mem_health,
        psu=psu_health,
        thermal=thermal_health,
        overall=overall,
    )
