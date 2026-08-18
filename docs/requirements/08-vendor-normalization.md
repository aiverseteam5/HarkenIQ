# Document 8: Vendor Normalization Specification

**Purpose:** Implementation-ready specification for the vendor abstraction layer that sits between raw Redfish API responses and the skill evaluation engine.
**Scope:** Dell iDRAC9/10 and HPE iLO5/6 normalization for R1 fault types (fan, disk, memory, PSU, thermal) plus event logs and health rollup.
**Status:** Draft.
**Depends on:** Document 5 (Redfish API Catalog), Document 6 (Agent Runtime Architecture).

---

## 1. Overview

### 1.1 Purpose

The vendor normalization layer is the single translation boundary between vendor-specific Redfish responses and the rest of the HarkenIQ agent. Every Redfish response passes through a normalizer before any skill, baseline, or reporting component touches it. The output is a set of Python dataclasses with stable, vendor-agnostic field names.

### 1.2 Design Principle

**Skills never see vendor-specific field names.** A skill definition that checks `fan.health == "Critical"` works identically on a Dell R750, Dell R760, HPE DL360 Gen10, and HPE DL380 Gen11. The normalizer absorbs all vendor differences so that every downstream consumer operates on a single, stable schema.

### 1.3 Architecture Position

```
Redfish API (vendor-specific JSON)
        │
        ▼
┌──────────────────────────┐
│   Vendor Normalizer      │  ← this document
│   (normalize.py)         │
│                          │
│   Input:  raw JSON dict  │
│   Output: dataclass      │
└──────────┬───────────────┘
           │
           ▼
  Normalized dataclasses
  (vendor-agnostic)
           │
     ┌─────┴──────┐
     ▼            ▼
  Skill        Baseline
  Engine       Tracker
```

### 1.4 Module Location

```
src/harkeniq/redfish/normalize.py    # Core normalizer logic + dataclass definitions
src/harkeniq/redfish/dell.py         # Dell OEM field extraction helpers
src/harkeniq/redfish/hpe.py          # HPE OEM field extraction helpers
```

---

## 2. Vendor Detection

### 2.1 Detection Method

Vendor detection runs during the agent startup sequence (Document 6, Section 14, steps 6-7). The normalizer identifies the vendor and controller generation from two Redfish calls that are already part of startup.

**Step 1: Identify vendor from service root.**

```
GET /redfish/v1/
```

Inspect the `Oem` namespace in the response:

| Condition | Vendor |
|-----------|--------|
| `Oem.Dell` key exists | Dell |
| `Oem.Hpe` key exists | HPE |
| Neither key exists | Unknown -- raise `UnsupportedVendorError` |

**Step 2: Identify controller generation from manager endpoint.**

```
GET /redfish/v1/Managers/{ManagerId}
```

| Vendor | Manager ID | Model Field | Values |
|--------|-----------|-------------|--------|
| Dell | `iDRAC.Embedded.1` | `Model` | `"iDRAC9"`, `"iDRAC10"` |
| HPE | `1` | `Model` | `"iLO 5"`, `"iLO 6"` |

Parse the controller type and version:

```python
# Dell
if manager_data["Model"] == "iDRAC9":
    controller_type = "iDRAC"
    controller_version = 9
elif manager_data["Model"] == "iDRAC10":
    controller_type = "iDRAC"
    controller_version = 10

# HPE
if manager_data["Model"] == "iLO 5":
    controller_type = "iLO"
    controller_version = 5
elif manager_data["Model"] == "iLO 6":
    controller_type = "iLO"
    controller_version = 6
```

**Step 3: Extract device identity.**

```
GET /redfish/v1/Systems/{SystemId}
```

| Field | Dell Source | HPE Source |
|-------|-----------|-----------|
| System model | `Model` | `Model` |
| Service tag / serial | `Oem.Dell.ServiceTag` (from Manager) or `SKU` (from System) | `SerialNumber` |
| Firmware version | `FirmwareVersion` (from Manager) | `Oem.Hpe.Firmware.Current.VersionString` (from Manager) |

### 2.2 Caching

Detection runs **once at startup**. The result is stored in a `DeviceIdentity` dataclass and cached for the lifetime of the agent session. The normalizer references this cached identity on every subsequent normalization call to select the correct field mapping path.

If the BMC is rebooted or firmware is updated while the agent is running, the agent must be restarted for re-detection. This is acceptable for R1 -- firmware updates require a server reboot, which will also restart the agent via systemd.

### 2.3 Resource ID Resolution

The normalizer abstracts vendor-specific resource IDs so internal code never hardcodes chassis or system IDs.

| Resource | Dell ID | HPE ID | Normalized Reference |
|----------|---------|--------|---------------------|
| Chassis | `System.Embedded.1` | `1` | `device.chassis_id` |
| System | `System.Embedded.1` | `1` | `device.system_id` |
| Manager | `iDRAC.Embedded.1` | `1` | `device.manager_id` |

These IDs are extracted during detection and stored in `DeviceIdentity`. All endpoint URL construction uses these stored IDs:

```python
thermal_url = f"/redfish/v1/Chassis/{device.chassis_id}/Thermal"
memory_url = f"/redfish/v1/Systems/{device.system_id}/Memory"
manager_url = f"/redfish/v1/Managers/{device.manager_id}"
```

---

## 3. Normalized Data Models

All normalized models are Python `dataclasses` with explicit type annotations. Each model carries an `oem_data` dict for vendor-specific fields that do not map to the normalized schema.

### 3.1 Top-Level Container

```python
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class NormalizedDevice:
    """Top-level container for all normalized data from a single server."""
    identity: DeviceIdentity
    fans: list[NormalizedFan] = field(default_factory=list)
    disks: list[NormalizedDisk] = field(default_factory=list)
    memory: list[NormalizedMemory] = field(default_factory=list)
    psus: list[NormalizedPSU] = field(default_factory=list)
    thermals: list[NormalizedThermal] = field(default_factory=list)
    power_metrics: Optional[NormalizedPowerMetrics] = None
    log_entries: list[NormalizedLogEntry] = field(default_factory=list)
    health_rollup: Optional[HealthRollup] = None
    collected_at: str = ""  # ISO 8601 timestamp of last poll
```

### 3.2 Device Identity

```python
@dataclass
class DeviceIdentity:
    """Vendor and device metadata, populated once at startup."""
    vendor: str               # "Dell" or "HPE"
    model: str                # e.g. "PowerEdge R750", "ProLiant DL360 Gen10"
    controller_type: str      # "iDRAC" or "iLO"
    controller_version: int   # 9, 10 (Dell) or 5, 6 (HPE)
    service_tag: str          # Dell ServiceTag or HPE SerialNumber
    firmware_version: str     # BMC firmware version string
    chassis_id: str           # "System.Embedded.1" (Dell) or "1" (HPE)
    system_id: str            # "System.Embedded.1" (Dell) or "1" (HPE)
    manager_id: str           # "iDRAC.Embedded.1" (Dell) or "1" (HPE)
    has_smart_storage: bool = False  # True if HPE iLO5 SmartStorage path exists
```

### 3.3 NormalizedFan

```python
@dataclass
class NormalizedFan:
    """Normalized fan sensor reading."""
    name: str = ""
    speed_rpm: Optional[int] = None
    speed_pct: Optional[int] = None       # Fan duty cycle %; Dell OEM only, HPE null
    health: str = "Unknown"
    state: str = "Unknown"
    threshold_low_critical: Optional[int] = None
    redundancy_health: str = "Unknown"
    location: str = ""
    oem_data: dict = field(default_factory=dict)
```

### 3.4 NormalizedDisk

```python
@dataclass
class NormalizedDisk:
    """Normalized physical drive."""
    name: str = ""                        # Drive model string
    serial: str = ""
    media_type: str = ""                  # "SSD" or "HDD"
    protocol: str = ""                    # "SATA", "SAS", "NVMe"
    capacity_bytes: Optional[int] = None
    health: str = "Unknown"
    life_left_pct: Optional[int] = None   # SSD only; 0-100, null for HDD
    smart_alert: bool = False             # SMART predictive failure
    raid_status: Optional[str] = None     # Dell only; null for HPE
    temperature_c: Optional[int] = None   # HPE OEM only; null for Dell
    slot: str = ""                        # Physical bay/slot identifier
    oem_data: dict = field(default_factory=dict)
```

### 3.5 NormalizedMemory

```python
@dataclass
class NormalizedMemory:
    """Normalized DIMM with ECC metrics."""
    name: str = ""                                # DIMM ID (e.g. "DIMM.Socket.A1")
    capacity_mib: Optional[int] = None
    type: str = ""                                # "DDR4" or "DDR5"
    speed_mhz: Optional[int] = None
    health: str = "Unknown"
    state: str = "Unknown"
    socket: Optional[int] = None                  # CPU socket number
    channel: Optional[int] = None                 # Memory channel number
    slot: Optional[int] = None                    # Slot within channel
    ecc_correctable_lifetime: Optional[int] = None
    ecc_uncorrectable_lifetime: Optional[int] = None
    ecc_correctable_current: Optional[int] = None
    alarm_ecc_correctable: bool = False
    alarm_ecc_uncorrectable: bool = False
    alarm_temperature: bool = False
    oem_data: dict = field(default_factory=dict)
```

### 3.6 NormalizedPSU

```python
@dataclass
class NormalizedPSU:
    """Normalized power supply unit."""
    name: str = ""
    member_id: str = ""
    type: str = ""                        # "AC", "DC"
    capacity_watts: Optional[int] = None
    output_watts: Optional[int] = None
    input_voltage: Optional[int] = None
    health: str = "Unknown"
    state: str = "Unknown"
    model: str = ""
    serial: str = ""
    redundancy_health: str = "Unknown"
    redundancy_mode: str = ""             # "Failover", "N+1"
    oem_data: dict = field(default_factory=dict)
```

### 3.7 NormalizedThermal

```python
@dataclass
class NormalizedThermal:
    """Normalized temperature sensor reading."""
    name: str = ""
    reading_c: Optional[float] = None
    health: str = "Unknown"
    threshold_warning: Optional[float] = None
    threshold_critical: Optional[float] = None
    threshold_fatal: Optional[float] = None       # Often null
    threshold_cold_warning: Optional[float] = None
    threshold_cold_critical: Optional[float] = None
    context: str = ""                              # Physical location
    oem_data: dict = field(default_factory=dict)
```

### 3.8 NormalizedPowerMetrics

```python
@dataclass
class NormalizedPowerMetrics:
    """System-level power consumption metrics from PowerControl."""
    system_power_watts: Optional[int] = None
    system_power_avg: Optional[int] = None
    system_power_peak: Optional[int] = None
    oem_data: dict = field(default_factory=dict)
```

### 3.9 NormalizedLogEntry

```python
@dataclass
class NormalizedLogEntry:
    """Normalized hardware event log entry."""
    id: str = ""
    timestamp: str = ""                   # ISO 8601
    severity: str = ""                    # "OK", "Warning", "Critical"
    message: str = ""
    message_id: str = ""                  # Structured message code
    component_id: Optional[str] = None    # Dell FQDD; null for HPE
    category: str = ""                    # Event category string
    oem_data: dict = field(default_factory=dict)
```

### 3.10 HealthRollup

```python
@dataclass
class HealthRollup:
    """System-wide health summary, one status per subsystem."""
    fan: str = "Unknown"
    disk: str = "Unknown"
    memory: str = "Unknown"
    psu: str = "Unknown"
    thermal: str = "Unknown"
    overall: str = "Unknown"
    # Each value is one of: "OK", "Warning", "Critical", "Unknown"
```

---

## 4. Field Mapping Tables

Each table maps normalized field names to their vendor-specific Redfish JSON paths. The "Default if Missing" column specifies the value the normalizer must assign when the source field is absent from the response.

### 4.1 Fan Field Mapping

Source endpoint: `GET /redfish/v1/Chassis/{ChassisId}/Thermal` -- `Fans[]` array.

| Normalized Field | Dell Redfish Path | HPE Redfish Path | HPE SmartStorage Path (iLO5) | Default if Missing |
|---|---|---|---|---|
| `name` | `Fans[].Name` | `Fans[].FanName` or `Fans[].Name` | N/A | `""` |
| `speed_rpm` | `Fans[].Reading` (when `ReadingUnits == "RPM"`) | `Fans[].Reading` (when `ReadingUnits == "RPM"`) | N/A | `None` |
| `speed_pct` | `Fans[].Oem.Dell.DellFan.FanPWM` | Not available | N/A | `None` |
| `health` | `Fans[].Status.Health` | `Fans[].Status.Health` | N/A | `"Unknown"` |
| `state` | `Fans[].Status.State` | `Fans[].Status.State` | N/A | `"Unknown"` |
| `threshold_low_critical` | `Fans[].LowerThresholdCritical` | `Fans[].LowerThresholdCritical` | N/A | `None` |
| `redundancy_health` | `Redundancy[0].Status.Health` | `Redundancy[0].Status.Health` | N/A | `"Unknown"` |
| `location` | `Fans[].PhysicalContext` | `Fans[].PhysicalContext` | N/A | `""` |

**Notes:**
- HPE may use `FanName` instead of `Name` on some iLO5 firmware versions. The normalizer checks `FanName` first, then falls back to `Name`.
- `speed_pct` is Dell-only (from `DellFan.FanPWM`). HPE does not expose fan duty cycle; this field is always `None` on HPE.
- `redundancy_health` is taken from the first `Redundancy` entry in the Thermal response. If multiple redundancy groups exist, use the first applicable one.

**OEM fields preserved in `oem_data`:**

| Vendor | OEM Field | Key in `oem_data` |
|--------|-----------|-------------------|
| Dell | `Fans[].Oem.Dell.DellFan.FanPWM` | `fan_pwm` |
| Dell | `Fans[].Oem.Dell.DellFan.HardwareType` | `hardware_type` |
| HPE | `Fans[].Oem.Hpe.Location` | `location_detail` |
| HPE | `Fans[].Oem.Hpe.HotPluggable` | `hot_pluggable` |

### 4.2 Disk Field Mapping

Source endpoint: `GET /redfish/v1/Systems/{SystemId}/Storage/{ControllerId}/Drives/{DriveId}`

For HPE iLO5 SmartStorage: `GET /redfish/v1/Systems/1/SmartStorage/ArrayControllers/{id}/DiskDrives/{DriveId}`

| Normalized Field | Dell Redfish Path | HPE Redfish Path (standard) | HPE SmartStorage Path (iLO5) | Default if Missing |
|---|---|---|---|---|
| `name` | `Model` | `Model` | `Model` | `""` |
| `serial` | `SerialNumber` | `SerialNumber` | `SerialNumber` | `""` |
| `media_type` | `MediaType` | `MediaType` | `MediaType` | `""` |
| `protocol` | `Protocol` | `Protocol` | `InterfaceType` | `""` |
| `capacity_bytes` | `CapacityBytes` | `CapacityBytes` | `CapacityMiB * 1048576` | `None` |
| `health` | `Status.Health` | `Status.Health` | `Status.Health` | `"Unknown"` |
| `life_left_pct` | `PredictedMediaLifeLeftPercent` | `PredictedMediaLifeLeftPercent` | `100 - SSDEnduranceUtilizationPercentage` | `None` |
| `smart_alert` | `FailurePredicted` OR `Oem.Dell.DellPhysicalDisk.SmartAlertIndication == "Yes"` | `FailurePredicted` | Not available | `False` |
| `raid_status` | `Oem.Dell.DellPhysicalDisk.RaidStatus` | Not available | Not available | `None` |
| `temperature_c` | Not available | `Oem.Hpe.CurrentTemperatureCelsius` | Not available | `None` |
| `slot` | `Oem.Dell.DellPhysicalDisk.Slot` | `Location` (physical location descriptor) | `Location` | `""` |

**Transformation rules:**

1. **`capacity_bytes` from SmartStorage:** HPE iLO5 SmartStorage returns `CapacityMiB` instead of `CapacityBytes`. The normalizer converts: `capacity_bytes = CapacityMiB * 1048576`.

2. **`life_left_pct` from SmartStorage:** HPE iLO5 SmartStorage returns `SSDEnduranceUtilizationPercentage`, which is the percentage of endurance *consumed* (0 = new, 100 = fully worn). The normalizer inverts this: `life_left_pct = 100 - SSDEnduranceUtilizationPercentage`. On iLO6 and Dell, `PredictedMediaLifeLeftPercent` is already the percentage *remaining* and needs no transformation.

3. **`smart_alert` on Dell:** Dell provides both `FailurePredicted` (standard Redfish boolean) and `Oem.Dell.DellPhysicalDisk.SmartAlertIndication` (string `"Yes"`/`"No"`). The normalizer sets `smart_alert = True` if either signal is true: `FailurePredicted == True or SmartAlertIndication == "Yes"`.

4. **`protocol` from SmartStorage:** HPE iLO5 SmartStorage uses `InterfaceType` (e.g., `"SAS"`, `"SATA"`) instead of `Protocol`. The normalizer maps `InterfaceType` to `protocol` directly -- the string values are the same.

**OEM fields preserved in `oem_data`:**

| Vendor | OEM Field | Key in `oem_data` |
|--------|-----------|-------------------|
| Dell | `Oem.Dell.DellPhysicalDisk.RaidStatus` | `raid_status` |
| Dell | `Oem.Dell.DellPhysicalDisk.RemainingRatedWriteEndurance` | `remaining_rated_write_endurance` |
| Dell | `Oem.Dell.DellPhysicalDisk.SmartAlertIndication` | `smart_alert_indication` |
| Dell | `Oem.Dell.DellPhysicalDisk.Slot` | `dell_slot` |
| HPE | `Oem.Hpe.CurrentTemperatureCelsius` | `current_temperature_celsius` |
| HPE | `Oem.Hpe.PowerOnHours` | `power_on_hours` |
| HPE | `Oem.Hpe.DriveStatus` | `drive_status` |
| HPE | `Oem.Hpe.SSDEnduranceUtilizationPercentage` | `ssd_endurance_utilization_pct` |

### 4.3 Memory Field Mapping

Source endpoints:
- DIMM properties: `GET /redfish/v1/Systems/{SystemId}/Memory/{DimmId}`
- ECC metrics: `GET /redfish/v1/Systems/{SystemId}/Memory/{DimmId}/MemoryMetrics`

| Normalized Field | Dell Redfish Path | HPE Redfish Path | HPE SmartStorage Path (iLO5) | Default if Missing |
|---|---|---|---|---|
| `name` | `Id` (e.g., `"DIMM.Socket.A1"`) | `Id` (e.g., `"proc1dimm1"`) | N/A | `""` |
| `capacity_mib` | `CapacityMiB` | `CapacityMiB` | N/A | `None` |
| `type` | `MemoryDeviceType` | `MemoryDeviceType` | N/A | `""` |
| `speed_mhz` | `OperatingSpeedMhz` | `OperatingSpeedMhz` | N/A | `None` |
| `health` | `Status.Health` | `Status.Health` | N/A | `"Unknown"` |
| `state` | `Status.State` | `Status.State` | N/A | `"Unknown"` |
| `socket` | `MemoryLocation.Socket` | `MemoryLocation.Socket` | N/A | `None` |
| `channel` | `MemoryLocation.Channel` | `MemoryLocation.Channel` | N/A | `None` |
| `slot` | `MemoryLocation.Slot` | `MemoryLocation.Slot` | N/A | `None` |
| `ecc_correctable_lifetime` | `LifeTime.CorrectableECCErrorCount` (from MemoryMetrics) | `LifeTime.CorrectableECCErrorCount` (from MemoryMetrics) | N/A | `None` |
| `ecc_uncorrectable_lifetime` | `LifeTime.UncorrectableECCErrorCount` (from MemoryMetrics) | `LifeTime.UncorrectableECCErrorCount` (from MemoryMetrics) | N/A | `None` |
| `ecc_correctable_current` | `CurrentPeriod.CorrectableECCErrorCount` (from MemoryMetrics) | `CurrentPeriod.CorrectableECCErrorCount` (from MemoryMetrics) | N/A | `None` |
| `alarm_ecc_correctable` | `HealthData.AlarmTrips.CorrectableECCError` (from MemoryMetrics) | `HealthData.AlarmTrips.CorrectableECCError` (from MemoryMetrics) | N/A | `False` |
| `alarm_ecc_uncorrectable` | `HealthData.AlarmTrips.UncorrectableECCError` (from MemoryMetrics) | `HealthData.AlarmTrips.UncorrectableECCError` (from MemoryMetrics) | N/A | `False` |
| `alarm_temperature` | `HealthData.AlarmTrips.Temperature` (from MemoryMetrics) | `HealthData.AlarmTrips.Temperature` (from MemoryMetrics) | N/A | `False` |

**Notes:**
- Memory properties and MemoryMetrics come from separate endpoints. The normalizer must join them by DIMM ID.
- Empty DIMM slots (`Status.State == "Absent"`) are included in the normalized output with health `"Unknown"` and all metric fields as `None`/`False`. Skills should filter on `state != "Absent"` for active-DIMM evaluation.
- The `MemoryDeviceType` field returns strings like `"DDR4"` or `"DDR5"`. These map directly to the normalized `type` field with no transformation.

**OEM fields preserved in `oem_data`:**

| Vendor | OEM Field | Key in `oem_data` |
|--------|-----------|-------------------|
| Dell | `Oem.Dell.DellMemory.BankLabel` | `bank_label` |
| Dell | `Oem.Dell.DellMemory.ManufactureDate` | `manufacture_date` |
| HPE | `Oem.Hpe.DIMMStatus` | `dimm_status` |

### 4.4 PSU Field Mapping

Source endpoint: `GET /redfish/v1/Chassis/{ChassisId}/Power` -- `PowerSupplies[]` array.

| Normalized Field | Dell Redfish Path | HPE Redfish Path | HPE SmartStorage Path (iLO5) | Default if Missing |
|---|---|---|---|---|
| `name` | `PowerSupplies[].Name` | `PowerSupplies[].Name` | N/A | `""` |
| `member_id` | `PowerSupplies[].MemberId` | `PowerSupplies[].MemberId` | N/A | `""` |
| `type` | `PowerSupplies[].PowerSupplyType` | `PowerSupplies[].PowerSupplyType` | N/A | `""` |
| `capacity_watts` | `PowerSupplies[].PowerCapacityWatts` | `PowerSupplies[].PowerCapacityWatts` | N/A | `None` |
| `output_watts` | `PowerSupplies[].LastPowerOutputWatts` | `PowerSupplies[].LastPowerOutputWatts` | N/A | `None` |
| `input_voltage` | `PowerSupplies[].LineInputVoltage` | `PowerSupplies[].LineInputVoltage` | N/A | `None` |
| `health` | `PowerSupplies[].Status.Health` | `PowerSupplies[].Status.Health` | N/A | `"Unknown"` |
| `state` | `PowerSupplies[].Status.State` | `PowerSupplies[].Status.State` | N/A | `"Unknown"` |
| `model` | `PowerSupplies[].Model` | `PowerSupplies[].Model` | N/A | `""` |
| `serial` | `PowerSupplies[].SerialNumber` | `PowerSupplies[].SerialNumber` | N/A | `""` |
| `redundancy_health` | `Redundancy[0].Status.Health` (from Power response) | `Redundancy[0].Status.Health` (from Power response) | N/A | `"Unknown"` |
| `redundancy_mode` | `Redundancy[0].Mode` (from Power response) | `Redundancy[0].Mode` (from Power response) | N/A | `""` |

**Notes:**
- `redundancy_health` and `redundancy_mode` come from the `Redundancy` array in the same `/Power` response, not from the individual `PowerSupplies[]` entries.
- All PSUs in the response share the same redundancy health. The normalizer copies the first `Redundancy` entry's health and mode to every `NormalizedPSU` in the collection.

**OEM fields preserved in `oem_data`:**

| Vendor | OEM Field | Key in `oem_data` |
|--------|-----------|-------------------|
| Dell | `Oem.Dell.DellPowerSupply.DetailedState` | `detailed_state` |
| Dell | `Oem.Dell.DellPowerSupply.Range1MaxInputPowerWatts` | `max_input_power_watts` |
| HPE | `Oem.Hpe.BayNumber` | `bay_number` |
| HPE | `Oem.Hpe.HotPluggable` | `hot_pluggable` |
| HPE | `Oem.Hpe.Mismatched` | `mismatched` |
| HPE | `Oem.Hpe.PowerSupplyStatus.State` | `psu_status_state` |

### 4.5 Thermal Field Mapping

Source endpoint: `GET /redfish/v1/Chassis/{ChassisId}/Thermal` -- `Temperatures[]` array.

| Normalized Field | Dell Redfish Path | HPE Redfish Path | HPE SmartStorage Path (iLO5) | Default if Missing |
|---|---|---|---|---|
| `name` | `Temperatures[].Name` | `Temperatures[].Name` | N/A | `""` |
| `reading_c` | `Temperatures[].ReadingCelsius` | `Temperatures[].ReadingCelsius` | N/A | `None` |
| `health` | `Temperatures[].Status.Health` | `Temperatures[].Status.Health` | N/A | `"Unknown"` |
| `threshold_warning` | `Temperatures[].UpperThresholdNonCritical` | `Temperatures[].UpperThresholdNonCritical` | N/A | `None` |
| `threshold_critical` | `Temperatures[].UpperThresholdCritical` | `Temperatures[].UpperThresholdCritical` | N/A | `None` |
| `threshold_fatal` | `Temperatures[].UpperThresholdFatal` | `Temperatures[].UpperThresholdFatal` | N/A | `None` |
| `threshold_cold_warning` | `Temperatures[].LowerThresholdNonCritical` | `Temperatures[].LowerThresholdNonCritical` | N/A | `None` |
| `threshold_cold_critical` | `Temperatures[].LowerThresholdCritical` | `Temperatures[].LowerThresholdCritical` | N/A | `None` |
| `context` | `Temperatures[].PhysicalContext` | `Temperatures[].PhysicalContext` | N/A | `""` |

**Notes:**
- Thermal field paths are identical between Dell and HPE. No vendor-specific branch is needed for the core fields.
- `threshold_fatal` is frequently `null` in real Redfish responses. Skills must handle `None` for this field.
- The normalizer does not filter sensors by `PhysicalContext`. All temperature sensors in the response are normalized. Skill definitions can filter by `context` if they only care about specific sensor locations (e.g., inlet temp vs CPU temp).

### 4.6 Power Metrics Field Mapping

Source endpoint: `GET /redfish/v1/Chassis/{ChassisId}/Power` -- `PowerControl[0]`.

| Normalized Field | Dell Redfish Path | HPE Redfish Path | HPE SmartStorage Path (iLO5) | Default if Missing |
|---|---|---|---|---|
| `system_power_watts` | `PowerControl[0].PowerConsumedWatts` | `PowerControl[0].PowerConsumedWatts` | N/A | `None` |
| `system_power_avg` | `PowerControl[0].PowerMetrics.AverageConsumedWatts` | `PowerControl[0].PowerMetrics.AverageConsumedWatts` | N/A | `None` |
| `system_power_peak` | `PowerControl[0].PowerMetrics.MaxConsumedWatts` | `PowerControl[0].PowerMetrics.MaxConsumedWatts` | N/A | `None` |

**Notes:**
- Power metrics paths are identical between Dell and HPE.
- `NormalizedPowerMetrics` is extracted from the same `/Power` response as PSUs. The normalizer produces both `list[NormalizedPSU]` and `NormalizedPowerMetrics` from a single API response.

### 4.7 Log Entry Field Mapping

Source endpoints:
- Dell hardware log: `GET /redfish/v1/Managers/iDRAC.Embedded.1/LogServices/Sel/Entries`
- HPE hardware log: `GET /redfish/v1/Systems/1/LogServices/IML/Entries`

| Normalized Field | Dell Redfish Path | HPE Redfish Path | HPE SmartStorage Path (iLO5) | Default if Missing |
|---|---|---|---|---|
| `id` | `Id` | `Id` | N/A | `""` |
| `timestamp` | `Created` | `Created` | N/A | `""` |
| `severity` | `Severity` | `Severity` | N/A | `""` |
| `message` | `Message` | `Message` | N/A | `""` |
| `message_id` | `MessageId` | `MessageId` | N/A | `""` |
| `component_id` | `Oem.Dell.DellSELEntry.FQDD` | Not available (see notes) | N/A | `None` |
| `category` | `Oem.Dell.DellSELEntry.Category` | `Oem.Hpe.Categories[0]` | N/A | `""` |

**Notes:**
- `component_id` is Dell-only. Dell provides the `FQDD` (Fully Qualified Device Descriptor) that identifies the exact component (e.g., `"Fan.Embedded.1A"`, `"Disk.Bay.0:Enclosure.Internal.0-1:RAID.Slot.1-1"`). HPE does not have a direct equivalent; `component_id` is `None` on HPE.
- `category` on HPE is extracted from `Oem.Hpe.Categories`, which is an array (e.g., `["Hardware", "Cooling"]`). The normalizer takes the first element. On Dell, `Oem.Dell.DellSELEntry.Category` is a single string (e.g., `"System Health"`).
- Log endpoint paths differ entirely between vendors. The normalizer must use the correct path based on `DeviceIdentity.vendor` and the stored `manager_id` / `system_id`.

**OEM fields preserved in `oem_data`:**

| Vendor | OEM Field | Key in `oem_data` |
|--------|-----------|-------------------|
| Dell | `Oem.Dell.DellSELEntry.FQDD` | `fqdd` |
| Dell | `Oem.Dell.DellSELEntry.DeviceType` | `device_type` |
| Dell | `Oem.Dell.DellSELEntry.Category` | `dell_category` |
| HPE | `Oem.Hpe.Class` | `class_code` |
| HPE | `Oem.Hpe.Code` | `event_code` |
| HPE | `Oem.Hpe.Categories` | `categories` |
| HPE | `Oem.Hpe.Count` | `occurrence_count` |
| HPE | `Oem.Hpe.Repaired` | `repaired` |

### 4.8 Health Rollup Field Mapping

Source endpoint: `GET /redfish/v1/Systems/{SystemId}`

| Normalized Field | Dell Redfish Path | HPE Redfish Path | Default if Missing |
|---|---|---|---|
| `overall` | `Status.HealthRollup` | `Status.HealthRollup` | `"Unknown"` |
| `memory` | `MemorySummary.Status.HealthRollup` | `MemorySummary.Status.HealthRollup` | `"Unknown"` |
| `fan` | Derived from `Thermal` poll (worst fan health) | `Oem.Hpe.AggregateHealthStatus.Fans` | `"Unknown"` |
| `disk` | Derived from `Storage` poll (worst disk health) | `Oem.Hpe.AggregateHealthStatus.Storage` | `"Unknown"` |
| `psu` | Derived from `Power` poll (worst PSU health) | `Oem.Hpe.AggregateHealthStatus.PowerSupplies` | `"Unknown"` |
| `thermal` | Derived from `Thermal` poll (worst thermal health) | `Oem.Hpe.AggregateHealthStatus.Temperatures` | `"Unknown"` |

**Notes:**
- Dell does not provide per-subsystem health rollups in the standard `/Systems/{id}` response for fan, disk, PSU, and thermal. Dell has a separate OEM endpoint (`/Dell/Systems/System.Embedded.1/DellRollupStatus`) but R1 derives subsystem health by computing `worst_health()` across the individual normalized sensor collections.
- HPE provides `Oem.Hpe.AggregateHealthStatus` on the System resource with per-subsystem booleans/status values. The normalizer maps these to the standard `"OK"` / `"Warning"` / `"Critical"` strings.
- The normalizer populates `HealthRollup` from both the system-level health rollup and per-subsystem computation, preferring the BMC-reported rollup when available.

---

## 5. Chassis/System ID Resolution

### 5.1 Problem

Dell and HPE use different identifier schemes for Chassis, System, and Manager resources:

| Resource | Dell | HPE |
|----------|------|-----|
| Chassis | `System.Embedded.1` | `1` |
| System | `System.Embedded.1` | `1` |
| Manager | `iDRAC.Embedded.1` | `1` |

Hardcoding either format would break on the other vendor's hardware.

### 5.2 Solution

The normalizer resolves IDs once during vendor detection (Section 2) and stores them in `DeviceIdentity.chassis_id`, `DeviceIdentity.system_id`, and `DeviceIdentity.manager_id`. All downstream code constructs Redfish URLs using these stored IDs.

**Resolution logic:**

```python
def resolve_resource_ids(service_root: dict, vendor: str) -> tuple[str, str, str]:
    """Extract chassis, system, and manager IDs from the service root."""
    # Parse from @odata.id links in the service root collections
    # or use known defaults per vendor.

    if vendor == "Dell":
        chassis_id = "System.Embedded.1"
        system_id = "System.Embedded.1"
        manager_id = "iDRAC.Embedded.1"
    elif vendor == "HPE":
        chassis_id = "1"
        system_id = "1"
        manager_id = "1"

    return chassis_id, system_id, manager_id
```

For R1, these are hardcoded per vendor because all four target devices use these exact IDs. If a future device uses different IDs (e.g., multi-chassis), the resolver should parse the `Members` array from `/redfish/v1/Chassis`, `/redfish/v1/Systems`, and `/redfish/v1/Managers` and select the first member. This is a known R2 enhancement.

### 5.3 URL Construction

All Redfish URL construction goes through a helper that uses the resolved IDs:

```python
class RedfishPaths:
    """Construct Redfish endpoint URLs using resolved resource IDs."""

    def __init__(self, identity: DeviceIdentity):
        self._identity = identity

    def thermal(self) -> str:
        return f"/redfish/v1/Chassis/{self._identity.chassis_id}/Thermal"

    def power(self) -> str:
        return f"/redfish/v1/Chassis/{self._identity.chassis_id}/Power"

    def storage(self) -> str:
        return f"/redfish/v1/Systems/{self._identity.system_id}/Storage"

    def memory(self) -> str:
        return f"/redfish/v1/Systems/{self._identity.system_id}/Memory"

    def memory_metrics(self, dimm_id: str) -> str:
        return f"/redfish/v1/Systems/{self._identity.system_id}/Memory/{dimm_id}/MemoryMetrics"

    def system(self) -> str:
        return f"/redfish/v1/Systems/{self._identity.system_id}"

    def manager(self) -> str:
        return f"/redfish/v1/Managers/{self._identity.manager_id}"

    def hw_log_entries(self) -> str:
        if self._identity.vendor == "Dell":
            return f"/redfish/v1/Managers/{self._identity.manager_id}/LogServices/Sel/Entries"
        elif self._identity.vendor == "HPE":
            return f"/redfish/v1/Systems/{self._identity.system_id}/LogServices/IML/Entries"

    def smart_storage_controllers(self) -> str:
        """HPE iLO5 only."""
        return f"/redfish/v1/Systems/{self._identity.system_id}/SmartStorage/ArrayControllers/"
```

---

## 6. HPE iLO5 SmartStorage Compatibility

### 6.1 Background

HPE iLO5 (ProLiant Gen10) uses a proprietary SmartStorage API for drives attached to HPE SmartArray RAID controllers. NVMe drives appear on the standard `/Storage` path, but SmartArray-attached SAS/SATA drives may only appear under `/SmartStorage`. iLO6 (Gen11) uses the standard `/Storage` path exclusively -- the SmartStorage path does not exist.

### 6.2 Detection

During startup, after vendor detection identifies HPE iLO5, the normalizer probes for SmartStorage:

```python
if identity.vendor == "HPE" and identity.controller_version == 5:
    response = await client.get("/redfish/v1/Systems/1/SmartStorage")
    if response.status == 200:
        identity.has_smart_storage = True
```

The `has_smart_storage` flag is stored in `DeviceIdentity` and persists for the session. The flag is `False` for Dell and HPE iLO6.

### 6.3 Merge Strategy

When `has_smart_storage` is `True`, the disk normalizer queries both paths and merges the results:

```
1. GET /redfish/v1/Systems/1/Storage  →  standard drives (NVMe, possibly some SAS/SATA)
2. GET /redfish/v1/Systems/1/SmartStorage/ArrayControllers/  →  SmartArray controllers
3. For each SmartArray controller:
     GET .../ArrayControllers/{id}/DiskDrives/  →  SmartStorage drives
4. Normalize standard drives  →  list A
5. Normalize SmartStorage drives  →  list B
6. Deduplicate by serial number (prefer standard path if duplicate)
7. Return merged list A + B
```

**Deduplication:** If the same physical drive appears on both the standard and SmartStorage paths (identified by matching `SerialNumber`), the standard path entry takes priority because its field names are already aligned with the normalized schema.

### 6.4 SmartStorage Field Differences

These fields require transformation when normalizing SmartStorage responses:

| Normalized Field | Standard Path Field | SmartStorage Field | Transformation |
|---|---|---|---|
| `protocol` | `Protocol` | `InterfaceType` | Direct mapping (same values: `"SAS"`, `"SATA"`) |
| `capacity_bytes` | `CapacityBytes` (integer, bytes) | `CapacityMiB` (integer, mebibytes) | `CapacityMiB * 1048576` |
| `life_left_pct` | `PredictedMediaLifeLeftPercent` (0-100, remaining) | `SSDEnduranceUtilizationPercentage` (0-100, consumed) | `100 - SSDEnduranceUtilizationPercentage` |

### 6.5 When SmartStorage Is Not Needed

- **Dell (all controllers):** SmartStorage path does not exist. Standard `/Storage` only.
- **HPE iLO6:** SmartStorage path does not exist. Standard `/Storage` only.
- **HPE iLO5 with NVMe only:** SmartStorage probe returns 404 or no ArrayControllers. The normalizer uses standard `/Storage` only.

---

## 7. OEM Field Preservation

### 7.1 Strategy

Every normalized dataclass includes an `oem_data: dict` field. The normalizer extracts vendor-specific OEM fields and stores them under short, readable keys in this dict. This preserves vendor-specific information that skills might need for advanced diagnosis without polluting the primary normalized interface.

### 7.2 Access Pattern

```python
# Primary interface -- vendor-agnostic, always use this
if disk.health == "Critical":
    ...

# OEM access -- vendor-specific, skill must guard with vendor check
if device.identity.vendor == "Dell" and disk.oem_data.get("raid_status") == "Rebuilding":
    ...
```

### 7.3 Skill Marking Convention

Skills that reference `oem_data` fields must declare a `vendor` constraint in their skill YAML definition:

```yaml
# In a skill definition
conditions:
  - field: disk.oem_data.raid_status
    operator: eq
    value: "Degraded"
    vendor: Dell  # This condition only evaluated on Dell devices
```

The skill evaluation engine skips conditions with a `vendor` constraint that does not match the current `DeviceIdentity.vendor`.

### 7.4 Completeness

The `oem_data` dict captures all OEM fields listed in Document 5 (Redfish API Catalog), Sections 2.3, 3.4, 4.5, 5.5, and 7.3. The full list is documented in the field mapping tables above (Sections 4.1 through 4.7 of this document).

The normalizer does **not** capture arbitrary/unknown OEM fields. Only OEM fields explicitly listed in the mapping tables are extracted. Unknown OEM fields are discarded. This prevents unbounded memory growth and keeps the `oem_data` dict predictable for testing.

---

## 8. Error Handling

### 8.1 Malformed Redfish Response

If a Redfish response cannot be parsed (invalid JSON, unexpected structure, missing required top-level keys like `Fans` or `Temperatures`), the normalizer:

1. Logs a WARNING with the endpoint URL and the parsing error.
2. Returns the corresponding normalized collection as an empty list.
3. Sets the relevant `HealthRollup` field to `"Unknown"`.

The normalizer does **not** raise an exception for malformed responses. The poll cycle continues, and the skill engine evaluates with whatever data is available.

**Example:** If `GET /Chassis/{id}/Thermal` returns valid JSON but the `Fans` key is missing, the normalizer returns `fans = []` and logs: `"Thermal response missing 'Fans' array"`.

### 8.2 Missing Endpoint

If a Redfish endpoint returns 404 (Not Found), the normalizer returns an empty collection for that subsystem. This is not an error -- it indicates the server does not have that component class.

| Endpoint | 404 Behavior |
|----------|-------------|
| `/Chassis/{id}/Thermal` | `fans = []`, `thermals = []` |
| `/Chassis/{id}/Power` | `psus = []`, `power_metrics = None` |
| `/Systems/{id}/Storage` | `disks = []` |
| `/Systems/{id}/Memory` | `memory = []` |
| `/Systems/{id}/Memory/{id}/MemoryMetrics` | ECC fields set to `None`/`False` on that DIMM |
| `/Systems/1/SmartStorage` | `has_smart_storage = False` |
| Log endpoints | `log_entries = []` |

### 8.3 Partial Response

If a Redfish response is structurally valid but individual items within a collection are missing fields, the normalizer:

1. Normalizes available fields using the mapping tables.
2. Applies defaults from Section 9 for any missing fields.
3. Does **not** skip or discard the partially-populated item.

**Example:** A drive entry that has `Status.Health` but no `PredictedMediaLifeLeftPercent` is normalized with `health = "OK"` and `life_left_pct = None`.

### 8.4 Connection Errors

The normalizer does **not** handle connection-level errors. These are the responsibility of the Redfish HTTP client (`redfish/client.py`):

| Error | Responsibility | Behavior |
|-------|---------------|----------|
| TCP connection refused | `client.py` | Raise `RedfishConnectionError` |
| TLS handshake failure | `client.py` | Raise `RedfishConnectionError` |
| HTTP timeout | `client.py` | Raise `RedfishTimeoutError` |
| HTTP 401 Unauthorized | `client.py` | Raise `RedfishAuthError` |
| HTTP 503 with `Retry-After` | `client.py` | Retry after indicated delay |
| HTTP 404 | `normalize.py` | Return empty collection (Section 8.2) |
| HTTP 200 with bad JSON | `normalize.py` | Return empty collection (Section 8.1) |
| HTTP 200 with valid JSON | `normalize.py` | Normalize normally |

### 8.5 Type Coercion Errors

If a field that should be numeric (e.g., `Reading`, `ReadingCelsius`) contains a non-numeric value, the normalizer:

1. Logs a WARNING with the field name, expected type, and actual value.
2. Sets the normalized field to its default (`None` for numeric fields).

The normalizer does **not** attempt to parse strings as numbers (e.g., `"42"` is not coerced to `42`). Redfish responses should use proper JSON types. If real hardware consistently returns strings for numeric fields, a vendor-specific workaround can be added to `dell.py` or `hpe.py`, documented as a known firmware quirk.

---

## 9. Null/Missing Field Defaults

When a field is absent from the Redfish response or explicitly set to `null` in JSON, the normalizer applies these defaults. These defaults are also embedded in the dataclass field definitions (Section 3) so that any `NormalizedFan()` constructed without arguments has safe default values.

### 9.1 Health and State Fields

| Field | Type | Default | Rationale |
|-------|------|---------|-----------|
| `health` | `str` | `"Unknown"` | Absence of health info is not the same as "OK". Skills must not assume healthy when data is missing. |
| `state` | `str` | `"Unknown"` | Same rationale as health. |
| `redundancy_health` | `str` | `"Unknown"` | Cannot assume redundancy is intact without confirmation. |

### 9.2 Numeric Fields

| Field | Type | Default | Rationale |
|-------|------|---------|-----------|
| `speed_rpm` | `Optional[int]` | `None` | Missing RPM should not be confused with 0 RPM (which means fan stopped). |
| `speed_pct` | `Optional[int]` | `None` | Dell-only; always None on HPE. |
| `threshold_low_critical` | `Optional[int]` | `None` | No threshold available. |
| `capacity_bytes` | `Optional[int]` | `None` | Unknown capacity. |
| `life_left_pct` | `Optional[int]` | `None` | Not applicable for HDD; unknown if missing on SSD. |
| `temperature_c` | `Optional[int]` | `None` | HPE-only; not available on Dell. |
| `capacity_mib` | `Optional[int]` | `None` | Unknown capacity. |
| `speed_mhz` | `Optional[int]` | `None` | Unknown speed. |
| `socket` | `Optional[int]` | `None` | Unknown location. |
| `channel` | `Optional[int]` | `None` | Unknown location. |
| `slot` (numeric) | `Optional[int]` | `None` | Unknown location. |
| `ecc_correctable_lifetime` | `Optional[int]` | `None` | Metrics endpoint may not exist. |
| `ecc_uncorrectable_lifetime` | `Optional[int]` | `None` | Metrics endpoint may not exist. |
| `ecc_correctable_current` | `Optional[int]` | `None` | Metrics endpoint may not exist. |
| `capacity_watts` | `Optional[int]` | `None` | Unknown capacity. |
| `output_watts` | `Optional[int]` | `None` | Unknown output. |
| `input_voltage` | `Optional[int]` | `None` | Unknown voltage. |
| `reading_c` | `Optional[float]` | `None` | Missing reading should not be confused with 0 degrees. |
| `threshold_warning` | `Optional[float]` | `None` | No threshold available. |
| `threshold_critical` | `Optional[float]` | `None` | No threshold available. |
| `threshold_fatal` | `Optional[float]` | `None` | Often null in real responses. |
| `threshold_cold_warning` | `Optional[float]` | `None` | No threshold available. |
| `threshold_cold_critical` | `Optional[float]` | `None` | No threshold available. |
| `system_power_watts` | `Optional[int]` | `None` | Unknown power draw. |
| `system_power_avg` | `Optional[int]` | `None` | Unknown average. |
| `system_power_peak` | `Optional[int]` | `None` | Unknown peak. |

### 9.3 Boolean Fields

| Field | Type | Default | Rationale |
|-------|------|---------|-----------|
| `smart_alert` | `bool` | `False` | No alert is the safe assumption when data is missing. |
| `alarm_ecc_correctable` | `bool` | `False` | No alarm tripped if data is missing. |
| `alarm_ecc_uncorrectable` | `bool` | `False` | No alarm tripped if data is missing. |
| `alarm_temperature` | `bool` | `False` | No alarm tripped if data is missing. |

### 9.4 String Fields

| Field | Type | Default | Rationale |
|-------|------|---------|-----------|
| `name` | `str` | `""` | Empty string rather than None for simpler display logic. |
| `serial` | `str` | `""` | Unknown serial. |
| `media_type` | `str` | `""` | Unknown media type. |
| `protocol` | `str` | `""` | Unknown protocol. |
| `slot` (string) | `str` | `""` | Unknown slot. |
| `type` (memory) | `str` | `""` | Unknown memory technology. |
| `member_id` | `str` | `""` | Unknown member. |
| `model` | `str` | `""` | Unknown model. |
| `location` | `str` | `""` | Unknown location. |
| `context` | `str` | `""` | Unknown physical context. |
| `redundancy_mode` | `str` | `""` | Unknown redundancy mode. |
| `id` (log) | `str` | `""` | Unknown entry ID. |
| `timestamp` | `str` | `""` | Unknown timestamp. |
| `severity` | `str` | `""` | Unknown severity (distinct from health -- this is a log attribute). |
| `message` | `str` | `""` | Empty message. |
| `message_id` | `str` | `""` | Unknown message code. |
| `category` | `str` | `""` | Unknown category. |
| `collected_at` | `str` | `""` | Populated by the poller, not the Redfish response. |

---

## 10. Normalizer API

### 10.1 Public Interface

The normalizer exposes one function per subsystem. Each takes raw Redfish JSON (as a Python dict) and the `DeviceIdentity`, and returns the corresponding normalized dataclass(es).

```python
# normalize.py -- public API

def detect_vendor(service_root: dict) -> str:
    """Detect vendor from /redfish/v1/ response. Returns 'Dell' or 'HPE'.
    Raises UnsupportedVendorError if neither Oem.Dell nor Oem.Hpe is found."""

def build_identity(
    service_root: dict,
    manager_data: dict,
    system_data: dict,
) -> DeviceIdentity:
    """Construct DeviceIdentity from startup Redfish responses."""

def normalize_fans(
    thermal_data: dict,
    identity: DeviceIdentity,
) -> list[NormalizedFan]:
    """Normalize Fans[] array from /Chassis/{id}/Thermal response."""

def normalize_thermals(
    thermal_data: dict,
    identity: DeviceIdentity,
) -> list[NormalizedThermal]:
    """Normalize Temperatures[] array from /Chassis/{id}/Thermal response."""

def normalize_disks(
    storage_data: list[dict],
    identity: DeviceIdentity,
    smart_storage_data: list[dict] | None = None,
) -> list[NormalizedDisk]:
    """Normalize drive data from /Storage and optionally /SmartStorage responses.
    storage_data: list of individual drive JSON responses.
    smart_storage_data: list of SmartStorage drive JSON responses (iLO5 only)."""

def normalize_memory(
    memory_data: list[dict],
    metrics_data: dict[str, dict],
    identity: DeviceIdentity,
) -> list[NormalizedMemory]:
    """Normalize DIMM data from /Memory responses joined with /MemoryMetrics.
    memory_data: list of individual DIMM JSON responses.
    metrics_data: dict mapping DIMM ID to its MemoryMetrics JSON response."""

def normalize_psus(
    power_data: dict,
    identity: DeviceIdentity,
) -> list[NormalizedPSU]:
    """Normalize PowerSupplies[] array from /Chassis/{id}/Power response."""

def normalize_power_metrics(
    power_data: dict,
    identity: DeviceIdentity,
) -> NormalizedPowerMetrics | None:
    """Normalize PowerControl[0] from /Chassis/{id}/Power response.
    Returns None if PowerControl array is empty or missing."""

def normalize_log_entries(
    entries_data: list[dict],
    identity: DeviceIdentity,
) -> list[NormalizedLogEntry]:
    """Normalize hardware event log entries."""

def normalize_health_rollup(
    system_data: dict,
    identity: DeviceIdentity,
    fans: list[NormalizedFan] | None = None,
    disks: list[NormalizedDisk] | None = None,
    psus: list[NormalizedPSU] | None = None,
    thermals: list[NormalizedThermal] | None = None,
) -> HealthRollup:
    """Build health rollup from system-level data and normalized collections.
    For Dell: derives subsystem health from normalized collections (worst-of).
    For HPE: uses Oem.Hpe.AggregateHealthStatus when available."""
```

### 10.2 Usage from Poller

The poller (Document 6, `poller.py`) calls the normalizer after each Redfish response:

```python
# In poller.py -- simplified example
thermal_json = await client.get(paths.thermal())
fans = normalize_fans(thermal_json, identity)
thermals = normalize_thermals(thermal_json, identity)

power_json = await client.get(paths.power())
psus = normalize_psus(power_json, identity)
power_metrics = normalize_power_metrics(power_json, identity)

# Assemble the device snapshot
device = NormalizedDevice(
    identity=identity,
    fans=fans,
    thermals=thermals,
    psus=psus,
    power_metrics=power_metrics,
    disks=disks,
    memory=memory_list,
    log_entries=log_entries,
    health_rollup=rollup,
    collected_at=datetime.utcnow().isoformat() + "Z",
)
```

### 10.3 Exceptions

The normalizer defines two custom exceptions:

```python
class UnsupportedVendorError(Exception):
    """Raised when vendor cannot be detected from the Redfish service root."""
    pass

class NormalizationError(Exception):
    """Raised on unrecoverable normalization failures (should not happen in practice).
    Malformed data is handled gracefully with defaults; this exception is a safety net."""
    pass
```

---

## 11. Testing Requirements

### 11.1 Unit Test Coverage

Every normalizer function must have unit tests covering:

1. **Dell happy path:** Valid Dell Redfish JSON in, correct normalized dataclass out.
2. **HPE happy path:** Valid HPE Redfish JSON in, correct normalized dataclass out.
3. **HPE iLO5 SmartStorage path:** SmartStorage JSON in, correct normalization with field transformations (capacity, life_left_pct, protocol).
4. **Missing fields:** JSON with various fields absent, verify defaults applied correctly.
5. **Empty collections:** Empty `Fans[]`, empty `PowerSupplies[]`, etc.
6. **Malformed data:** Invalid structure, verify graceful degradation (empty list, logged warning).
7. **OEM field preservation:** Verify `oem_data` dict is populated correctly for both vendors.

### 11.2 Test Fixtures

Test fixtures should be extracted from the mock simulator fixtures (Document 5, Section 11.1). Each test uses real-shaped Redfish JSON responses from:

```
tests/fixtures/dell_r750/
tests/fixtures/dell_r760/
tests/fixtures/hpe_dl360_gen10/
tests/fixtures/hpe_dl380_gen11/
```

### 11.3 Property-Based Tests

For field default coverage, consider property-based tests (e.g., Hypothesis) that randomly remove fields from valid fixture JSON and verify that the normalizer never raises an exception and always returns a valid dataclass.
