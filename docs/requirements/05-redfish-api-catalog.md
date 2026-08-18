# Document 5: Redfish API Catalog

**Purpose:** Implementation-ready endpoint reference for the 5 R1 fault types across 4 target devices.
**Scope:** Dell iDRAC9 (R750), Dell iDRAC10 (R760), HPE iLO5 (DL360 Gen10), HPE iLO6 (DL380 Gen11).
**Status:** Draft -- verify against live hardware in Week 1.

---

## 1. Design Decisions

### 1.1 Use Legacy Redfish Endpoints for R1

Both Dell and HPE added new Redfish 2021+ subsystem models (`ThermalSubsystem`, `PowerSubsystem`) on their latest controllers while keeping the legacy endpoints alive. **R1 uses legacy endpoints only.** They work on all four target devices and reduce the normalization surface.

| Model | Legacy (R1) | New Subsystem (R2+) |
|-------|-------------|---------------------|
| Thermal | `/Chassis/{id}/Thermal` | `/Chassis/{id}/ThermalSubsystem/` |
| Power | `/Chassis/{id}/Power` | `/Chassis/{id}/PowerSubsystem/` |

### 1.2 Chassis and System IDs

| Vendor | Chassis ID | System ID | Manager ID |
|--------|-----------|-----------|------------|
| Dell | `System.Embedded.1` | `System.Embedded.1` | `iDRAC.Embedded.1` |
| HPE | `1` | `1` | `1` |

### 1.3 Polling Strategy

- **Primary transport:** HTTP polling (settled decision: polling is primary, SSE is opportunistic).
- **Recommended interval:** 60 seconds for sensor data, 300 seconds for inventory/logs.
- **Rate limits:** Dell throttles at ~15s minimum between requests to the same endpoint (returns 503 with `Retry-After`). HPE similar. Agent must respect `Retry-After`.
- **Authentication:** HTTP Basic Auth over HTTPS (both vendors). Session auth (`X-Auth-Token`) preferred for polling loops to avoid per-request auth overhead.

### 1.4 Licensing Requirements

| Capability | Dell License | HPE License |
|-----------|-------------|-------------|
| Read-only polling (all endpoints below) | Basic/Express (free) | Standard (free) |
| Event subscriptions (SSE/push) | Enterprise | Advanced |
| Telemetry streaming (280+ metrics) | Datacenter | N/A (iLO6 TelemetryService partial) |
| Configuration changes (PATCH) | Enterprise | Advanced |
| Firmware update | Enterprise | Advanced |

**R1 requires only the free tier for read-only polling.** Event subscriptions (R2) require Enterprise/Advanced.

---

## 2. Fault Type 1: Fan

### 2.1 Endpoint

```
GET /redfish/v1/Chassis/{ChassisId}/Thermal
```

### 2.2 Response Structure (Fans Array)

| Property | Type | Description | Dell | HPE |
|----------|------|-------------|------|-----|
| `Fans[].Name` or `FanName` | string | Human-readable name | `"System Board Fan1A"` | `"Fan 1"` |
| `Fans[].Reading` | integer | Current speed | RPM | RPM |
| `Fans[].ReadingUnits` | string | Unit of Reading | `"RPM"` | `"RPM"` |
| `Fans[].Status.Health` | string | Health state | `OK/Warning/Critical` | `OK/Warning/Critical` |
| `Fans[].Status.State` | string | Operational state | `Enabled/Absent` | `Enabled/Absent` |
| `Fans[].LowerThresholdCritical` | integer | Min RPM before critical | Present | Present |
| `Fans[].UpperThresholdCritical` | integer | Max RPM threshold | Often null | Present |
| `Fans[].PhysicalContext` | string | Location context | `"SystemBoard"` | `"Backplane"`, `"SystemBoard"` |

### 2.3 OEM Extensions

**Dell:**
- `Fans[].Oem.Dell.DellFan.FanPWM` -- fan duty cycle percentage (0-100)
- `Fans[].Oem.Dell.DellFan.HardwareType` -- e.g. `"System Board Fan"`

**HPE:**
- `Fans[].Oem.Hpe.Location` -- fan location string
- `Fans[].Oem.Hpe.HotPluggable` -- boolean

### 2.4 Redundancy

Same response includes a `Redundancy` array:
- `Redundancy[].Mode` -- `"N+1"` or `"Failover"`
- `Redundancy[].Status.Health` -- `"OK"` when redundant
- `Redundancy[].MinNumNeeded` -- minimum fans for redundancy

### 2.5 Fault Detection Rules

| Condition | Signal | Severity |
|-----------|--------|----------|
| Fan failed | `Status.Health == "Critical"` AND `Status.State == "Enabled"` | CRITICAL |
| Fan removed | `Status.State == "Absent"` | CRITICAL |
| Fan degraded | `Status.Health == "Warning"` | WARNING |
| Redundancy lost | `Redundancy[].Status.Health != "OK"` | WARNING |
| RPM below threshold | `Reading < LowerThresholdCritical` | CRITICAL |
| RPM trending down | Rate-of-change on `Reading` over baseline window | TRENDING |

### 2.6 Normalization

```yaml
normalized_key: fan
fields:
  name: Fans[].Name || Fans[].FanName
  speed_rpm: Fans[].Reading
  speed_pct: Fans[].Oem.Dell.DellFan.FanPWM  # Dell only; HPE null
  health: Fans[].Status.Health
  state: Fans[].Status.State
  threshold_low_critical: Fans[].LowerThresholdCritical
  redundancy_health: Redundancy[0].Status.Health
  location: Fans[].PhysicalContext
```

---

## 3. Fault Type 2: Disk / Storage

### 3.1 Endpoints

**Storage controllers:**
```
GET /redfish/v1/Systems/{SystemId}/Storage
GET /redfish/v1/Systems/{SystemId}/Storage/{ControllerId}
```

**Physical drives:**
```
GET /redfish/v1/Systems/{SystemId}/Storage/{ControllerId}/Drives/{DriveId}
```

**HPE iLO5 additional path (SmartStorage -- does NOT exist on iLO6):**
```
GET /redfish/v1/Systems/1/SmartStorage/ArrayControllers/
GET /redfish/v1/Systems/1/SmartStorage/ArrayControllers/{id}/DiskDrives/
GET /redfish/v1/Systems/1/SmartStorage/ArrayControllers/{id}/DiskDrives/{DriveId}
```

### 3.2 Controller IDs

| Vendor | Controller ID Format | Example |
|--------|---------------------|---------|
| Dell | `RAID.Slot.{n}-{n}` | `RAID.Slot.1-1` |
| Dell NVMe | `CPU.1` | `CPU.1` |
| HPE iLO5 | Numeric or RAID string | `DE009000` |
| HPE iLO6 | Numeric or MR/SR string | `DE00A000` |

### 3.3 Drive Properties

| Property | Type | Description | Dell | HPE (std) | HPE (SmartStorage, iLO5 only) |
|----------|------|-------------|------|-----------|-------------------------------|
| `MediaType` | string | Drive type | `"SSD"`, `"HDD"` | `"SSD"`, `"HDD"` | `"SSD"`, `"HDD"` |
| `Protocol` | string | Interface | `"SATA"`, `"SAS"`, `"NVMe"` | Same | `InterfaceType` field instead |
| `CapacityBytes` | integer | Size in bytes | Present | Present | `CapacityMiB` instead |
| `Status.Health` | string | Drive health | `OK/Warning/Critical` | Same | Same |
| `PredictedMediaLifeLeftPercent` | integer | SSD wear (0-100) | Present (SSD) | Present (SSD) | `SSDEnduranceUtilizationPercentage` |
| `FailurePredicted` | boolean | SMART trip | Present | May be null | N/A |
| `RotationSpeedRPM` | integer | HDD spin speed | Present | Present | `RotationalSpeedRpm` |
| `Model` | string | Drive model | Present | Present | Present |
| `SerialNumber` | string | Serial | Present | Present | Present |

### 3.4 OEM Extensions

**Dell:**
- `Oem.Dell.DellPhysicalDisk.RaidStatus` -- `"Online"`, `"Degraded"`, `"Failed"`, `"Rebuilding"`, `"Ready"`, `"Foreign"`, `"Offline"`
- `Oem.Dell.DellPhysicalDisk.RemainingRatedWriteEndurance` -- SSD endurance (0-100)
- `Oem.Dell.DellPhysicalDisk.SmartAlertIndication` -- `"Yes"` / `"No"`
- `Oem.Dell.DellPhysicalDisk.Slot` -- physical bay number

**HPE (standard Redfish path):**
- `Oem.Hpe.CurrentTemperatureCelsius` -- drive temperature
- `Oem.Hpe.PowerOnHours` -- drive age
- `Oem.Hpe.DriveStatus` -- extended status
- `Oem.Hpe.SSDEnduranceUtilizationPercentage` -- may appear here on iLO6

### 3.5 HPE SmartStorage Compatibility Layer

The agent MUST handle iLO5's SmartStorage path. Detection strategy:

```python
# Try standard Redfish first (works on Dell, HPE iLO6, HPE iLO5 for NVMe)
response = GET /redfish/v1/Systems/{id}/Storage/

# If HPE iLO5 and SmartArray drives missing from standard path:
if vendor == "HPE" and ilo_version == 5:
    smartstorage = GET /redfish/v1/Systems/1/SmartStorage/ArrayControllers/
    # Merge SmartStorage drives into normalized model
```

### 3.6 Fault Detection Rules

| Condition | Signal | Severity |
|-----------|--------|----------|
| Drive failed | `Status.Health == "Critical"` | CRITICAL |
| SMART predictive failure | `FailurePredicted == true` OR Dell `SmartAlertIndication == "Yes"` | WARNING |
| SSD wear critical | `PredictedMediaLifeLeftPercent < 10` | CRITICAL |
| SSD wear warning | `PredictedMediaLifeLeftPercent < 25` | WARNING |
| RAID degraded | Dell `RaidStatus == "Degraded"` | WARNING |
| Drive rebuilding | Dell `RaidStatus == "Rebuilding"` | WARNING |
| SSD wear trending | Rate-of-change on `PredictedMediaLifeLeftPercent` | TRENDING |

### 3.7 Normalization

```yaml
normalized_key: disk
fields:
  name: Model
  serial: SerialNumber
  media_type: MediaType
  protocol: Protocol || InterfaceType  # SmartStorage uses InterfaceType
  capacity_bytes: CapacityBytes || (CapacityMiB * 1048576)  # SmartStorage uses MiB
  health: Status.Health
  life_left_pct: PredictedMediaLifeLeftPercent || (100 - SSDEnduranceUtilizationPercentage)
  smart_alert: FailurePredicted || (SmartAlertIndication == "Yes")
  raid_status: Oem.Dell.DellPhysicalDisk.RaidStatus  # Dell only
  temperature_c: Oem.Hpe.CurrentTemperatureCelsius  # HPE only
  slot: Oem.Dell.DellPhysicalDisk.Slot || Location
```

---

## 4. Fault Type 3: Memory

### 4.1 Endpoints

**DIMM inventory:**
```
GET /redfish/v1/Systems/{SystemId}/Memory
GET /redfish/v1/Systems/{SystemId}/Memory/{DimmId}
```

**ECC error counts:**
```
GET /redfish/v1/Systems/{SystemId}/Memory/{DimmId}/MemoryMetrics
```

### 4.2 DIMM IDs

| Vendor | DIMM ID Format | Example |
|--------|---------------|---------|
| Dell | `DIMM.Socket.{letter}{number}` | `DIMM.Socket.A1` |
| HPE | `proc{n}dimm{n}` or numeric | `proc1dimm1` |

### 4.3 Memory Properties

| Property | Type | Description | Both Vendors |
|----------|------|-------------|-------------|
| `MemoryDeviceType` | string | Technology | `"DDR4"` (Gen10/R750) or `"DDR5"` (Gen11/R760) |
| `CapacityMiB` | integer | DIMM size | Present |
| `OperatingSpeedMhz` | integer | Current speed | Present |
| `Manufacturer` | string | DIMM vendor | Present |
| `PartNumber` | string | Part number | Present |
| `SerialNumber` | string | Serial | Present |
| `ErrorCorrection` | string | ECC type | `"MultiBitECC"` |
| `Status.Health` | string | Health | `OK/Warning/Critical` |
| `Status.State` | string | State | `Enabled/Absent` (empty slots = Absent) |
| `MemoryLocation.Socket` | integer | CPU socket | Present |
| `MemoryLocation.Channel` | integer | Memory channel | Present |
| `MemoryLocation.Slot` | integer | Slot within channel | Present |

### 4.4 MemoryMetrics Properties

| Property | Type | Description |
|----------|------|-------------|
| `HealthData.AlarmTrips.CorrectableECCError` | boolean | Correctable error threshold tripped |
| `HealthData.AlarmTrips.UncorrectableECCError` | boolean | Uncorrectable error detected |
| `HealthData.AlarmTrips.Temperature` | boolean | DIMM thermal alarm |
| `CurrentPeriod.CorrectableECCErrorCount` | integer | Correctable errors since last clear |
| `CurrentPeriod.UncorrectableECCErrorCount` | integer | Uncorrectable errors since last clear |
| `LifeTime.CorrectableECCErrorCount` | integer | Total lifetime correctable errors |
| `LifeTime.UncorrectableECCErrorCount` | integer | Total lifetime uncorrectable errors |

**Availability:** iDRAC9 firmware 4.x+, all iDRAC10, iLO5, iLO6.

### 4.5 OEM Extensions

**Dell:**
- `Oem.Dell.DellMemory.BankLabel` -- memory bank
- `Oem.Dell.DellMemory.ManufactureDate` -- e.g. `"2023-Wk42"`

**HPE:**
- `Oem.Hpe.DIMMStatus` -- HPE-specific DIMM status string

### 4.6 Fault Detection Rules

| Condition | Signal | Severity |
|-----------|--------|----------|
| DIMM failed | `Status.Health == "Critical"` | CRITICAL |
| Uncorrectable ECC | `HealthData.AlarmTrips.UncorrectableECCError == true` | CRITICAL |
| Correctable ECC threshold | `HealthData.AlarmTrips.CorrectableECCError == true` | WARNING |
| DIMM thermal alarm | `HealthData.AlarmTrips.Temperature == true` | WARNING |
| DIMM degraded | `Status.Health == "Warning"` | WARNING |
| ECC rate trending up | Rate-of-change on `LifeTime.CorrectableECCErrorCount` | TRENDING |

### 4.7 Normalization

```yaml
normalized_key: memory
fields:
  name: Id  # e.g. "DIMM.Socket.A1" or "proc1dimm1"
  capacity_mib: CapacityMiB
  type: MemoryDeviceType  # DDR4 or DDR5
  speed_mhz: OperatingSpeedMhz
  health: Status.Health
  state: Status.State
  socket: MemoryLocation.Socket
  channel: MemoryLocation.Channel
  slot: MemoryLocation.Slot
  ecc_correctable_lifetime: LifeTime.CorrectableECCErrorCount
  ecc_uncorrectable_lifetime: LifeTime.UncorrectableECCErrorCount
  ecc_correctable_current: CurrentPeriod.CorrectableECCErrorCount
  alarm_ecc_correctable: HealthData.AlarmTrips.CorrectableECCError
  alarm_ecc_uncorrectable: HealthData.AlarmTrips.UncorrectableECCError
  alarm_temperature: HealthData.AlarmTrips.Temperature
```

---

## 5. Fault Type 4: Power Supply (PSU)

### 5.1 Endpoint

```
GET /redfish/v1/Chassis/{ChassisId}/Power
```

### 5.2 Response Structure (PowerSupplies Array)

| Property | Type | Description | Dell | HPE |
|----------|------|-------------|------|-----|
| `PowerSupplies[].Name` | string | PSU name | `"PS1 Status"` | `"HpeServerPowerSupply"` |
| `PowerSupplies[].MemberId` | string | Index | `"0"`, `"1"` | `"0"`, `"1"` |
| `PowerSupplies[].PowerSupplyType` | string | Type | `"AC"` | `"AC"` |
| `PowerSupplies[].PowerCapacityWatts` | integer | Rated capacity | Present | Present |
| `PowerSupplies[].LastPowerOutputWatts` | integer | Current output | Present | Present |
| `PowerSupplies[].PowerInputWatts` | integer | Current input | Present (Dell) | May be absent |
| `PowerSupplies[].LineInputVoltage` | integer | Input voltage | Present | Present |
| `PowerSupplies[].LineInputVoltageType` | string | Voltage type | `"ACHighLine"` | `"ACHighLine"` |
| `PowerSupplies[].EfficiencyPercent` | float | PSU efficiency | Present (some FW) | Absent |
| `PowerSupplies[].Status.Health` | string | Health | `OK/Warning/Critical` | Same |
| `PowerSupplies[].Status.State` | string | State | `Enabled/Absent` | Same |
| `PowerSupplies[].Model` | string | PSU model | Present | Present |
| `PowerSupplies[].SerialNumber` | string | Serial | Present | Present |
| `PowerSupplies[].FirmwareVersion` | string | FW version | Present | Present |

### 5.3 System Power Metrics

Same `/Power` response includes `PowerControl` array:

| Property | Type | Description |
|----------|------|-------------|
| `PowerControl[0].PowerConsumedWatts` | integer | Total server power draw |
| `PowerControl[0].PowerCapacityWatts` | integer | Total power budget |
| `PowerControl[0].PowerMetrics.AverageConsumedWatts` | integer | Average over interval |
| `PowerControl[0].PowerMetrics.MaxConsumedWatts` | integer | Peak over interval |
| `PowerControl[0].PowerMetrics.MinConsumedWatts` | integer | Trough over interval |
| `PowerControl[0].PowerMetrics.IntervalInMin` | integer | Metrics window (minutes) |

### 5.4 Power Redundancy

Same response, `Redundancy` array:
- `Redundancy[].Mode` -- `"Failover"` or `"N+1"`
- `Redundancy[].Status.Health` -- `"OK"` when redundant
- `Redundancy[].Status.State` -- `"Enabled"`, `"Disabled"`
- `Redundancy[].MinNumNeeded` -- min PSUs for redundancy

### 5.5 OEM Extensions

**Dell:**
- `Oem.Dell.DellPowerSupply.DetailedState` -- e.g. `"Presence Detected"`
- `Oem.Dell.DellPowerSupply.Range1MaxInputPowerWatts` -- max rated input

**HPE:**
- `Oem.Hpe.BayNumber` -- physical bay
- `Oem.Hpe.HotPluggable` -- boolean
- `Oem.Hpe.Mismatched` -- boolean (mismatched PSU pair)
- `Oem.Hpe.PowerSupplyStatus.State` -- HPE-specific state string

### 5.6 Fault Detection Rules

| Condition | Signal | Severity |
|-----------|--------|----------|
| PSU failed | `Status.Health == "Critical"` | CRITICAL |
| PSU removed | `Status.State == "Absent"` | CRITICAL |
| PSU degraded | `Status.Health == "Warning"` | WARNING |
| Redundancy lost | `Redundancy[].Status.Health != "OK"` | WARNING |
| PSU mismatch | HPE `Oem.Hpe.Mismatched == true` | WARNING |
| Voltage out of range | `LineInputVoltage` outside rated range | WARNING |
| Power draw trending up | Rate-of-change on `PowerConsumedWatts` | TRENDING |

### 5.7 Normalization

```yaml
normalized_key: psu
fields:
  name: PowerSupplies[].Name
  member_id: PowerSupplies[].MemberId
  type: PowerSupplies[].PowerSupplyType
  capacity_watts: PowerSupplies[].PowerCapacityWatts
  output_watts: PowerSupplies[].LastPowerOutputWatts
  input_voltage: PowerSupplies[].LineInputVoltage
  health: PowerSupplies[].Status.Health
  state: PowerSupplies[].Status.State
  model: PowerSupplies[].Model
  serial: PowerSupplies[].SerialNumber
  redundancy_health: Redundancy[0].Status.Health
  redundancy_mode: Redundancy[0].Mode
  system_power_watts: PowerControl[0].PowerConsumedWatts
  system_power_avg: PowerControl[0].PowerMetrics.AverageConsumedWatts
  system_power_peak: PowerControl[0].PowerMetrics.MaxConsumedWatts
```

---

## 6. Fault Type 5: Thermal (Temperature)

### 6.1 Endpoint

```
GET /redfish/v1/Chassis/{ChassisId}/Thermal
```

(Same endpoint as fans -- response contains both `Fans` and `Temperatures` arrays.)

### 6.2 Response Structure (Temperatures Array)

| Property | Type | Description | Both Vendors |
|----------|------|-------------|-------------|
| `Temperatures[].Name` | string | Sensor name | Present |
| `Temperatures[].ReadingCelsius` | float | Current temp | Present |
| `Temperatures[].Status.Health` | string | Health | `OK/Warning/Critical` |
| `Temperatures[].Status.State` | string | State | `Enabled` |
| `Temperatures[].UpperThresholdNonCritical` | float | Warning threshold | Present |
| `Temperatures[].UpperThresholdCritical` | float | Critical threshold | Present |
| `Temperatures[].UpperThresholdFatal` | float | Fatal / shutdown | Often null |
| `Temperatures[].LowerThresholdNonCritical` | float | Cold warning | Present |
| `Temperatures[].LowerThresholdCritical` | float | Cold critical | Present |
| `Temperatures[].PhysicalContext` | string | Sensor location | Present |

### 6.3 Common Sensor Names

**Dell (R750/R760):**
- `System Board Inlet Temp` -- ambient intake air
- `System Board Exhaust Temp` -- exhaust air
- `CPU1 Temp`, `CPU2 Temp` -- processor die
- `DIMM A1 Temp`, `DIMM B1 Temp` -- memory
- `System Board PCH Temp` -- Platform Controller Hub

**HPE (DL360 Gen10 / DL380 Gen11):**
- `01-Inlet Ambient` -- intake air
- `02-CPU 1`, `03-CPU 2` -- processor die
- `04-P1 DIMM 1-6`, `05-P1 DIMM 7-12` -- memory zones
- `08-HD Max` -- hottest drive
- `11-PS 1 Inlet`, `12-PS 2 Inlet` -- PSU intake
- `30-System Board` -- board sensor

### 6.4 Fault Detection Rules

| Condition | Signal | Severity |
|-----------|--------|----------|
| Thermal shutdown imminent | `ReadingCelsius >= UpperThresholdFatal` (if non-null) | CRITICAL |
| Over critical threshold | `ReadingCelsius >= UpperThresholdCritical` | CRITICAL |
| Over warning threshold | `ReadingCelsius >= UpperThresholdNonCritical` | WARNING |
| Under cold critical | `ReadingCelsius <= LowerThresholdCritical` | WARNING |
| Sensor reports critical | `Status.Health == "Critical"` | CRITICAL |
| Temperature trending up | Rate-of-change on `ReadingCelsius` toward threshold | TRENDING |

### 6.5 Normalization

```yaml
normalized_key: thermal
fields:
  name: Temperatures[].Name
  reading_c: Temperatures[].ReadingCelsius
  health: Temperatures[].Status.Health
  threshold_warning: Temperatures[].UpperThresholdNonCritical
  threshold_critical: Temperatures[].UpperThresholdCritical
  threshold_fatal: Temperatures[].UpperThresholdFatal
  threshold_cold_warning: Temperatures[].LowerThresholdNonCritical
  threshold_cold_critical: Temperatures[].LowerThresholdCritical
  context: Temperatures[].PhysicalContext
```

---

## 7. Event Logs

### 7.1 Log Endpoints

| Log Type | Dell Path | HPE Path |
|----------|----------|----------|
| Hardware event log | `/redfish/v1/Managers/iDRAC.Embedded.1/LogServices/Sel/Entries` | `/redfish/v1/Systems/1/LogServices/IML/Entries` |
| Lifecycle / management log | `/redfish/v1/Managers/iDRAC.Embedded.1/LogServices/Lclog/Entries` | `/redfish/v1/Managers/1/LogServices/IEL/Entries` |

### 7.2 Log Entry Properties

| Property | Type | Description |
|----------|------|-------------|
| `Id` | string | Entry ID |
| `Created` | string | ISO 8601 timestamp |
| `Severity` | string | `"OK"`, `"Warning"`, `"Critical"` |
| `Message` | string | Human-readable description |
| `MessageId` | string | Structured message code |
| `EntryType` | string | `"SEL"` (Dell) or `"Oem"` (HPE) |

### 7.3 OEM Log Properties

**Dell:**
- `Oem.Dell.DellSELEntry.Category` -- `"System Health"`, `"Storage"`, etc.
- `Oem.Dell.DellSELEntry.FQDD` -- exact component identifier (e.g. `"Fan.Embedded.1A"`, `"Disk.Bay.0:..."`)
- `Oem.Dell.DellSELEntry.DeviceType` -- `"Fan"`, `"PSU"`, `"Disk"`, etc.

**HPE:**
- `Oem.Hpe.Class` -- numeric class code (17 = environment, 2 = storage)
- `Oem.Hpe.Code` -- numeric event code
- `Oem.Hpe.Categories` -- array like `["Hardware", "Cooling", "Power"]`
- `Oem.Hpe.Count` -- occurrences
- `Oem.Hpe.Repaired` -- boolean (event cleared)

### 7.4 Querying

**Pagination (both vendors):**
```
GET .../Entries?$top=50&$skip=0
```

**Filtering (iDRAC9 5.x+, iDRAC10, iLO5/6 with limitations):**
```
GET .../Entries?$filter=Severity eq 'Critical'
```

**R1 recommendation:** Client-side filtering after paginated fetch. `$filter` support is inconsistent across firmware versions.

### 7.5 Log Normalization

```yaml
normalized_key: event
fields:
  id: Id
  timestamp: Created
  severity: Severity
  message: Message
  message_id: MessageId
  component_id: Oem.Dell.DellSELEntry.FQDD || null  # Dell only
  category: Oem.Dell.DellSELEntry.Category || Oem.Hpe.Categories[0]
```

---

## 8. Health Rollup

### 8.1 Quick Health Check

Both vendors provide a system-level health rollup:

```
GET /redfish/v1/Systems/{SystemId}
```

| Property | Description |
|----------|-------------|
| `Status.Health` | Overall system health |
| `Status.HealthRollup` | Rolled-up health across subsystems |
| `MemorySummary.Status.HealthRollup` | Memory subsystem |
| `ProcessorSummary.Status.HealthRollup` | CPU subsystem |

### 8.2 OEM Health Rollups

**Dell:**
```
GET /redfish/v1/Dell/Systems/System.Embedded.1/DellRollupStatus
```
Per-subsystem: CPU, Memory, PSU, Fan, Storage, Battery -- each with `RollupStatus` of `OK`, `Degraded`, `Error`.

**HPE:**
```
GET /redfish/v1/Systems/1/
```
Under `Oem.Hpe.AggregateHealthStatus`:
- `BiosOrHardwareHealth`, `FanRedundancy`, `Fans`, `Memory`, `PowerSupplies`, `PowerSupplyRedundancy`, `Processors`, `Storage`, `Temperatures`

### 8.3 R1 Usage

Poll the health rollup as a fast-path "anything wrong?" check at 60-second intervals. Only drill into specific subsystem endpoints when rollup shows `Degraded` or `Critical`. This reduces API call volume.

---

## 9. Vendor Detection

### 9.1 How to Identify the Device

```
GET /redfish/v1/
```

Check for OEM namespace:
- Dell: `Oem.Dell` present
- HPE: `Oem.Hpe` present

```
GET /redfish/v1/Managers/{ManagerId}
```

| Vendor | Property | Example Values |
|--------|----------|----------------|
| Dell | `Model` | `"iDRAC9"`, `"iDRAC10"` |
| HPE | `Model` | `"iLO 5"`, `"iLO 6"` |
| Both | `FirmwareVersion` | Version string |
| Dell | `Oem.Dell.ServiceTag` | Asset tag |
| HPE | `Oem.Hpe.Firmware.Current.VersionString` | FW version |
| HPE | `Oem.Hpe.License.LicenseType` | License tier |

### 9.2 Agent Startup Sequence

```
1. GET /redfish/v1/                           → detect vendor (Dell/HPE) and Redfish version
2. GET /redfish/v1/Managers/{id}              → detect controller generation (iDRAC9/10, iLO5/6)
3. GET /redfish/v1/Systems/{id}               → get system model, health rollup
4. GET /redfish/v1/Systems/{id}/Storage       → enumerate storage controllers and drives
5. GET /redfish/v1/Systems/{id}/Memory        → enumerate DIMMs
6. GET /redfish/v1/Chassis/{id}/Thermal       → enumerate fans and temp sensors
7. GET /redfish/v1/Chassis/{id}/Power         → enumerate PSUs
```

Steps 4-7 establish the baseline inventory. Subsequent polls check for changes.

---

## 10. Cross-Vendor Differences Summary

### 10.1 What's the Same (code-once)

- Thermal endpoint path and response structure (Fans + Temperatures arrays)
- Power endpoint path and response structure (PowerSupplies + PowerControl)
- Memory endpoint paths and MemoryMetrics structure
- Health status values (`OK`, `Warning`, `Critical`)
- State values (`Enabled`, `Absent`, `Disabled`)
- System-level health rollup location
- Session authentication flow

### 10.2 What Differs (needs vendor branch)

| Area | Dell | HPE |
|------|------|-----|
| Chassis ID | `System.Embedded.1` | `1` |
| System ID | `System.Embedded.1` | `1` |
| Manager ID | `iDRAC.Embedded.1` | `1` |
| OEM namespace | `Oem.Dell` | `Oem.Hpe` |
| DIMM ID format | `DIMM.Socket.A1` | `proc1dimm1` |
| Drive ID format | `Disk.Bay.0:Enclosure...` | Numeric/hash |
| RAID status | `Oem.Dell.DellPhysicalDisk.RaidStatus` | Not in standard path |
| SMART alert | `FailurePredicted` + Dell OEM | `FailurePredicted` |
| SSD wear (iLO5 SmartStorage) | N/A | `SSDEnduranceUtilizationPercentage` |
| Hardware event log path | `.../iDRAC.Embedded.1/LogServices/Sel/Entries` | `.../Systems/1/LogServices/IML/Entries` |
| Component ID in logs | `FQDD` field | Class + Code numbers |
| Health rollup OEM | `DellRollupStatus` | `AggregateHealthStatus` |
| Storage on iLO5 | N/A | SmartStorage path required for SmartArray drives |

### 10.3 iLO5 SmartStorage Handling

This is the only structural divergence requiring a separate code path:

```
if vendor == "HPE" and controller_version == "iLO5":
    # Check standard /Storage first (NVMe drives appear here)
    # Then also query /SmartStorage for SmartArray-attached drives
    # Normalize SSDEnduranceUtilizationPercentage → PredictedMediaLifeLeftPercent
    #   (invert: life_left = 100 - endurance_utilization)
```

iLO6 uses standard `/Storage` exclusively. No SmartStorage path exists.

---

## 11. Mock Simulator Requirements

The Redfish mock simulator for R1 must serve responses for all endpoints above. Per device:

### 11.1 Fixture Files Needed (per device model)

| Fixture | Endpoint |
|---------|----------|
| `service_root.json` | `GET /redfish/v1/` |
| `manager.json` | `GET /redfish/v1/Managers/{id}` |
| `system.json` | `GET /redfish/v1/Systems/{id}` |
| `thermal.json` | `GET /redfish/v1/Chassis/{id}/Thermal` |
| `power.json` | `GET /redfish/v1/Chassis/{id}/Power` |
| `storage_collection.json` | `GET /redfish/v1/Systems/{id}/Storage` |
| `storage_controller.json` | `GET /redfish/v1/Systems/{id}/Storage/{cid}` |
| `drive_{n}.json` | `GET /redfish/v1/Systems/{id}/Storage/{cid}/Drives/{did}` |
| `memory_collection.json` | `GET /redfish/v1/Systems/{id}/Memory` |
| `memory_{dimm}.json` | `GET /redfish/v1/Systems/{id}/Memory/{did}` |
| `memory_metrics_{dimm}.json` | `GET /redfish/v1/Systems/{id}/Memory/{did}/MemoryMetrics` |
| `sel_entries.json` | `GET .../LogServices/Sel/Entries` (Dell) or `IML/Entries` (HPE) |

Plus for iLO5 only:
| `smartstorage_controllers.json` | `GET /redfish/v1/Systems/1/SmartStorage/ArrayControllers` |
| `smartstorage_drives.json` | `GET .../DiskDrives` |

### 11.2 Fault Injection

The mock simulator must support injecting faults by modifying fixture state:

```python
# Example: inject fan failure on Dell R750
simulator.inject_fault("dell-r750", "fan", {
    "target": "System Board Fan1A",
    "health": "Critical",
    "reading": 0,  # RPM drops to 0
    "redundancy_health": "Warning"  # redundancy degraded
})
```

Minimum 10 test paths: 5 fault types x 2 vendors. Each fault must produce the correct `Status.Health`, `Status.State`, and threshold violations in the fixture response.

---

## 12. Open Items for Week 1

1. **Verify against live hardware.** This catalog is from published documentation. Property names and threshold values should be validated against actual iDRAC9, iDRAC10, iLO5, and iLO6 responses.
2. **Confirm MemoryMetrics availability** on the design partner's specific firmware versions.
3. **Confirm iLO5 SmartStorage** behavior -- does the standard `/Storage` path show SmartArray drives at all, or only NVMe?
4. **Document actual threshold values** from real hardware (e.g., inlet temp warning at 42C vs 47C varies by server configuration).
5. **Confirm `$filter` support** on log entries for the design partner's firmware versions.
6. **Confirm HPE license tier** -- does the design partner have iLO Advanced? (Needed for R2 event subscriptions, not R1 polling.)
