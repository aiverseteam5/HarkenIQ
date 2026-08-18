# Document 11: Mock Simulator Specification

**Purpose:** Implementation-ready specification for the HarkenIQ Redfish mock simulator.
**Scope:** HTTPS mock server serving Redfish API fixture responses for 4 target devices, with fault injection, state management, session authentication, and peer heartbeat simulation.
**Status:** Draft.
**Depends on:** Doc 5 (Redfish API Catalog), Doc 6 (Agent Runtime Architecture), Doc 9 (Demo Scenario).

---

## 1. Overview

The mock simulator is an HTTPS server that serves Redfish API responses from JSON fixture files. It replaces live BMC hardware during development, testing, and the `harken demo` showcase.

### 1.1 Use Cases

| Use Case | Description |
|----------|-------------|
| Development | Developers run the simulator locally to build and debug agent polling, normalization, and skill evaluation without BMC hardware. |
| Unit testing | Tests import the simulator as a Python module and start/stop it programmatically per test case. |
| Integration testing | The full agent polls the simulator over HTTPS, exercising the Redfish client, normalization, skill engine, and verdict pipeline end-to-end against all 22 test paths (Doc 12). |
| End-to-end testing | CI runs `harken demo --speed 10` against the simulator and asserts exit code 0. |
| `harken demo` | The 60-second automated showcase (Doc 9) runs entirely against the simulator with scripted fault injection. |

### 1.2 Simulated Devices

| Device Profile | Server Model | BMC Controller | Redfish Flavor |
|----------------|-------------|----------------|----------------|
| `dell-r750` | Dell PowerEdge R750 | iDRAC9 | Dell Oem.Dell namespace, System.Embedded.1 IDs |
| `dell-r760` | Dell PowerEdge R760 | iDRAC10 | Dell Oem.Dell namespace, System.Embedded.1 IDs |
| `hpe-dl360-gen10` | HPE ProLiant DL360 Gen10 | iLO 5 | HPE Oem.Hpe namespace, numeric IDs, SmartStorage |
| `hpe-dl380-gen11` | HPE ProLiant DL380 Gen11 | iLO 6 | HPE Oem.Hpe namespace, numeric IDs, standard Storage only |

### 1.3 Design Constraints

- The simulator is a development and test tool. It is NOT a general-purpose Redfish emulator.
- It only serves the endpoints HarkenIQ R1 actually polls (Doc 5, Sections 2-9).
- Fixture data must be realistic enough to validate normalization and skill evaluation but does not need to match a specific serial number or firmware version.
- The simulator ships as part of the `harkeniq` Python package under `harkeniq.mock`.

---

## 2. Architecture

### 2.1 Technology Stack

| Component | Technology | Rationale |
|-----------|-----------|-----------|
| HTTP server | `aiohttp` | Already used by the agent's async runtime (Doc 6). No additional dependency. |
| TLS | Self-signed certificate | BMC access is always HTTPS. The agent's Redfish client must handle self-signed certs (verify=False). |
| Fixture storage | Static JSON files on disk | Easy to inspect, version control, and diff. |
| Runtime state | In-memory Python dicts | Mutable copies of fixture data. Fault injection modifies the in-memory state, not the files. |
| Fault injection API | Same aiohttp server, `/test/` prefix | Non-Redfish endpoints for test control. |

### 2.2 Component Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                    Mock Simulator Process                    │
│                                                             │
│  ┌──────────────────────┐    ┌───────────────────────────┐  │
│  │   Fixture Loader     │    │    State Manager          │  │
│  │                      │    │                           │  │
│  │  Reads JSON files    │───▶│  In-memory device state   │  │
│  │  from disk at        │    │  per device profile.      │  │
│  │  startup.             │    │  Fault injection mutates  │  │
│  │                      │    │  this state.              │  │
│  └──────────────────────┘    └─────────┬─────────────────┘  │
│                                        │                    │
│  ┌──────────────────────┐    ┌─────────▼─────────────────┐  │
│  │   Session Manager    │    │    Route Handler          │  │
│  │                      │    │                           │  │
│  │  Token issuance,     │    │  Maps Redfish paths to    │  │
│  │  validation, expiry  │    │  state lookups. Returns   │  │
│  │                      │    │  JSON responses.          │  │
│  └──────────────────────┘    └───────────────────────────┘  │
│                                                             │
│  ┌──────────────────────┐    ┌───────────────────────────┐  │
│  │   Gradual Faults     │    │    Peer Simulator         │  │
│  │                      │    │                           │  │
│  │  asyncio tasks that  │    │  Sends UDP heartbeat      │  │
│  │  change values over  │    │  packets on configured    │  │
│  │  time (e.g., RPM     │    │  intervals. Can be        │  │
│  │  decline).           │    │  started/stopped.         │  │
│  └──────────────────────┘    └───────────────────────────┘  │
│                                                             │
│  ┌──────────────────────────────────────────────────────┐   │
│  │   HTTPS Listener (aiohttp + self-signed TLS)         │   │
│  │   Serves both Redfish endpoints and /test/ control   │   │
│  └──────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

### 2.3 Self-Signed Certificate

At first startup, the simulator generates a self-signed TLS certificate and key pair if they do not already exist:

| File | Location | Purpose |
|------|----------|---------|
| `mock_cert.pem` | `~/.harkeniq/mock/` | Self-signed X.509 certificate |
| `mock_key.pem` | `~/.harkeniq/mock/` | RSA 2048-bit private key |

The certificate uses `CN=harkeniq-mock` with a 365-day validity period. The agent's Redfish client connects with `ssl=False` (equivalent to `verify=False` in requests) since all BMC connections use self-signed certificates.

---

## 3. CLI Interface

The mock simulator is invoked through the `harken mock` subcommand group.

### 3.1 Commands

```
harken mock start [OPTIONS]
harken mock stop
harken mock status
```

### 3.2 `harken mock start`

Start one or more mock BMC simulators.

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--device DEVICE` | string | `dell-r750` | Device profile to simulate. One of: `dell-r750`, `dell-r760`, `hpe-dl360-gen10`, `hpe-dl380-gen11`, `all`. |
| `--port PORT` | integer | `8443` | HTTPS port to bind. When `--device all`, the first device binds to PORT and subsequent devices bind to PORT+1, PORT+2, PORT+3. |
| `--host HOST` | string | `0.0.0.0` | Bind address. |
| `--no-auth` | flag | `false` | Disable session authentication. All Redfish requests are accepted without X-Auth-Token. Useful for quick testing. |
| `--credentials USER:PASS` | string | `admin:password` | Set the valid username and password for session authentication. |
| `--log-level LEVEL` | string | `info` | Logging verbosity: `debug`, `info`, `warning`, `error`. |
| `--foreground` | flag | `false` | Run in foreground (default is to daemonize). Always foreground when started programmatically from tests. |

**Port assignment in multi-device mode (`--device all`):**

| Port | Device Profile |
|------|---------------|
| 8443 | `dell-r750` |
| 8444 | `dell-r760` |
| 8445 | `hpe-dl360-gen10` |
| 8446 | `hpe-dl380-gen11` |

**Example usage:**

```bash
# Start a single Dell R750 simulator
harken mock start --device dell-r750 --port 8443

# Start all 4 devices
harken mock start --device all

# Start without authentication for quick testing
harken mock start --device dell-r750 --no-auth

# Start with custom credentials
harken mock start --device dell-r750 --credentials "root:calvin"
```

### 3.3 `harken mock stop`

Stop all running mock simulators started by the current user.

```bash
harken mock stop
# Output: Stopped 4 simulator(s).
```

Sends SIGTERM to the simulator process(es). The simulator performs a clean shutdown (closes sockets, cancels asyncio tasks).

### 3.4 `harken mock status`

Show running simulators and their state.

```bash
harken mock status
```

**Example output:**

```
DEVICE             PORT   PID     STATE    FAULTS
dell-r750          8443   12345   running  1 active (fan)
dell-r760          8444   12346   running  healthy
hpe-dl360-gen10    8445   12347   running  healthy
hpe-dl380-gen11    8446   12348   running  healthy
```

### 3.5 Programmatic API

For unit and integration tests, the simulator is importable as a Python module:

```python
from harkeniq.mock.simulator import MockSimulator

async def test_fan_fault():
    sim = MockSimulator(device="dell-r750", port=18443, no_auth=True)
    await sim.start()
    try:
        # Test code polls https://localhost:18443/redfish/v1/...
        await sim.inject_fault("fan", target="Fan1A", params={"health": "Critical", "speed_rpm": 0})
        # Assert agent detects the fault
    finally:
        await sim.stop()
```

The programmatic API uses ephemeral ports (starting at 18443) to avoid conflicts with CLI-started simulators.

---

## 4. Endpoint Routing

### 4.1 Routing Strategy

The simulator maintains a routing table that maps HTTP method + Redfish URI path to a handler function. Each handler reads from the in-memory device state (not directly from fixture files) and returns JSON.

Routes are device-profile-specific because Dell and HPE use different resource IDs (e.g., `System.Embedded.1` vs `1`).

### 4.2 Dell Routing Table (dell-r750, dell-r760)

Dell devices use these resource IDs:
- Chassis ID: `System.Embedded.1`
- System ID: `System.Embedded.1`
- Manager ID: `iDRAC.Embedded.1`

| Method | Path | Fixture File | Description |
|--------|------|-------------|-------------|
| GET | `/redfish/v1/` | `service_root.json` | ServiceRoot with Oem.Dell namespace |
| GET | `/redfish/v1/Managers/iDRAC.Embedded.1` | `manager.json` | Manager info (iDRAC9 or iDRAC10 model) |
| GET | `/redfish/v1/Systems/System.Embedded.1` | `system.json` | System info with health rollup |
| GET | `/redfish/v1/Chassis/System.Embedded.1/Thermal` | `thermal.json` | Fans array + Temperatures array + Redundancy |
| GET | `/redfish/v1/Chassis/System.Embedded.1/Power` | `power.json` | PowerSupplies + PowerControl + Redundancy |
| GET | `/redfish/v1/Systems/System.Embedded.1/Storage` | `storage_collection.json` | Storage controller collection |
| GET | `/redfish/v1/Systems/System.Embedded.1/Storage/RAID.Slot.1-1` | `storage_controller_raid.json` | RAID controller with Drives links |
| GET | `/redfish/v1/Systems/System.Embedded.1/Storage/RAID.Slot.1-1/Drives/Disk.Bay.0:Enclosure.Internal.0-1:RAID.Slot.1-1` | `drive_0.json` | Drive 0 (SSD) |
| GET | `/redfish/v1/Systems/System.Embedded.1/Storage/RAID.Slot.1-1/Drives/Disk.Bay.1:Enclosure.Internal.0-1:RAID.Slot.1-1` | `drive_1.json` | Drive 1 (SSD) |
| GET | `/redfish/v1/Systems/System.Embedded.1/Storage/RAID.Slot.1-1/Drives/Disk.Bay.2:Enclosure.Internal.0-1:RAID.Slot.1-1` | `drive_2.json` | Drive 2 (SSD) |
| GET | `/redfish/v1/Systems/System.Embedded.1/Storage/RAID.Slot.1-1/Drives/Disk.Bay.3:Enclosure.Internal.0-1:RAID.Slot.1-1` | `drive_3.json` | Drive 3 (SSD) |
| GET | `/redfish/v1/Systems/System.Embedded.1/Memory` | `memory_collection.json` | Memory DIMM collection |
| GET | `/redfish/v1/Systems/System.Embedded.1/Memory/DIMM.Socket.A1` | `memory_dimm_a1.json` | DIMM A1 properties |
| GET | `/redfish/v1/Systems/System.Embedded.1/Memory/DIMM.Socket.A2` | `memory_dimm_a2.json` | DIMM A2 properties |
| GET | `/redfish/v1/Systems/System.Embedded.1/Memory/DIMM.Socket.B1` | `memory_dimm_b1.json` | DIMM B1 properties |
| GET | `/redfish/v1/Systems/System.Embedded.1/Memory/DIMM.Socket.B2` | `memory_dimm_b2.json` | DIMM B2 properties |
| GET | `/redfish/v1/Systems/System.Embedded.1/Memory/DIMM.Socket.A1/MemoryMetrics` | `memory_metrics_a1.json` | DIMM A1 ECC counts |
| GET | `/redfish/v1/Systems/System.Embedded.1/Memory/DIMM.Socket.A2/MemoryMetrics` | `memory_metrics_a2.json` | DIMM A2 ECC counts |
| GET | `/redfish/v1/Systems/System.Embedded.1/Memory/DIMM.Socket.B1/MemoryMetrics` | `memory_metrics_b1.json` | DIMM B1 ECC counts |
| GET | `/redfish/v1/Systems/System.Embedded.1/Memory/DIMM.Socket.B2/MemoryMetrics` | `memory_metrics_b2.json` | DIMM B2 ECC counts |
| GET | `/redfish/v1/Managers/iDRAC.Embedded.1/LogServices/Sel/Entries` | `sel_entries.json` | Hardware event log entries |
| POST | `/redfish/v1/SessionService/Sessions` | (dynamic) | Create session, return X-Auth-Token |
| DELETE | `/redfish/v1/SessionService/Sessions/{session_id}` | (dynamic) | Delete session |

**Note on DIMM count:** The demo scenario (Doc 9) specifies 16x 32GB DIMMs. The fixture collection file (`memory_collection.json`) lists all 16 DIMMs with `@odata.id` links. For brevity, the routing table above shows 4 representative DIMMs (A1, A2, B1, B2). The simulator registers routes for all 16 DIMM IDs: `DIMM.Socket.A1` through `DIMM.Socket.A8` and `DIMM.Socket.B1` through `DIMM.Socket.B8`. A single fixture template generates all 16 with slot-specific IDs and serial numbers.

### 4.3 HPE Routing Table (hpe-dl360-gen10, hpe-dl380-gen11)

HPE devices use numeric resource IDs:
- Chassis ID: `1`
- System ID: `1`
- Manager ID: `1`

| Method | Path | Fixture File | Description |
|--------|------|-------------|-------------|
| GET | `/redfish/v1/` | `service_root.json` | ServiceRoot with Oem.Hpe namespace |
| GET | `/redfish/v1/Managers/1` | `manager.json` | Manager info (iLO 5 or iLO 6 model) |
| GET | `/redfish/v1/Systems/1` | `system.json` | System info with AggregateHealthStatus |
| GET | `/redfish/v1/Chassis/1/Thermal` | `thermal.json` | Fans + Temperatures with HPE naming |
| GET | `/redfish/v1/Chassis/1/Power` | `power.json` | PowerSupplies + PowerControl |
| GET | `/redfish/v1/Systems/1/Storage` | `storage_collection.json` | Storage controller collection |
| GET | `/redfish/v1/Systems/1/Storage/DE00A000` | `storage_controller.json` | Storage controller |
| GET | `/redfish/v1/Systems/1/Storage/DE00A000/Drives/0` | `drive_0.json` | Drive 0 |
| GET | `/redfish/v1/Systems/1/Storage/DE00A000/Drives/1` | `drive_1.json` | Drive 1 |
| GET | `/redfish/v1/Systems/1/Storage/DE00A000/Drives/2` | `drive_2.json` | Drive 2 |
| GET | `/redfish/v1/Systems/1/Storage/DE00A000/Drives/3` | `drive_3.json` | Drive 3 |
| GET | `/redfish/v1/Systems/1/Memory` | `memory_collection.json` | Memory DIMM collection |
| GET | `/redfish/v1/Systems/1/Memory/proc1dimm1` | `memory_proc1dimm1.json` | DIMM properties |
| GET | `/redfish/v1/Systems/1/Memory/proc1dimm2` | `memory_proc1dimm2.json` | DIMM properties |
| GET | `/redfish/v1/Systems/1/Memory/proc2dimm1` | `memory_proc2dimm1.json` | DIMM properties |
| GET | `/redfish/v1/Systems/1/Memory/proc2dimm2` | `memory_proc2dimm2.json` | DIMM properties |
| GET | `/redfish/v1/Systems/1/Memory/proc1dimm1/MemoryMetrics` | `memory_metrics_proc1dimm1.json` | ECC counts |
| GET | `/redfish/v1/Systems/1/Memory/proc1dimm2/MemoryMetrics` | `memory_metrics_proc1dimm2.json` | ECC counts |
| GET | `/redfish/v1/Systems/1/Memory/proc2dimm1/MemoryMetrics` | `memory_metrics_proc2dimm1.json` | ECC counts |
| GET | `/redfish/v1/Systems/1/Memory/proc2dimm2/MemoryMetrics` | `memory_metrics_proc2dimm2.json` | ECC counts |
| GET | `/redfish/v1/Systems/1/LogServices/IML/Entries` | `iml_entries.json` | Integrated Management Log entries |
| POST | `/redfish/v1/SessionService/Sessions` | (dynamic) | Create session |
| DELETE | `/redfish/v1/SessionService/Sessions/{session_id}` | (dynamic) | Delete session |

**Note on DIMM count:** Same approach as Dell. 16 DIMMs total: `proc1dimm1` through `proc1dimm8` and `proc2dimm1` through `proc2dimm8`.

### 4.4 HPE iLO5 SmartStorage Routes (hpe-dl360-gen10 only)

In addition to the standard routes above, the `hpe-dl360-gen10` profile registers these SmartStorage endpoints. These do NOT exist on iLO6 (`hpe-dl380-gen11`).

| Method | Path | Fixture File | Description |
|--------|------|-------------|-------------|
| GET | `/redfish/v1/Systems/1/SmartStorage/ArrayControllers` | `smartstorage_controllers.json` | SmartArray controller collection |
| GET | `/redfish/v1/Systems/1/SmartStorage/ArrayControllers/0` | `smartstorage_controller_0.json` | SmartArray controller 0 |
| GET | `/redfish/v1/Systems/1/SmartStorage/ArrayControllers/0/DiskDrives` | `smartstorage_drives_collection.json` | Disk drive collection |
| GET | `/redfish/v1/Systems/1/SmartStorage/ArrayControllers/0/DiskDrives/0` | `smartstorage_drive_0.json` | SmartStorage drive 0 |
| GET | `/redfish/v1/Systems/1/SmartStorage/ArrayControllers/0/DiskDrives/1` | `smartstorage_drive_1.json` | SmartStorage drive 1 |

SmartStorage drives use different property names than standard Redfish drives (see Doc 5, Section 3.3). The fixture files reflect this: `CapacityMiB` instead of `CapacityBytes`, `InterfaceType` instead of `Protocol`, `SSDEnduranceUtilizationPercentage` instead of `PredictedMediaLifeLeftPercent`.

### 4.5 Unrouted Paths

Any GET request to a path not in the routing table returns:

```json
{
    "error": {
        "@Message.ExtendedInfo": [
            {
                "Message": "The resource at the URI /redfish/v1/... was not found.",
                "MessageId": "Base.1.0.GeneralError",
                "Severity": "Critical"
            }
        ]
    }
}
```

HTTP status: 404 Not Found.

---

## 5. Fixture File Format

### 5.1 Directory Structure

```
harkeniq/mock/fixtures/
├── dell-r750/
│   ├── service_root.json
│   ├── manager.json
│   ├── system.json
│   ├── thermal.json
│   ├── power.json
│   ├── storage_collection.json
│   ├── storage_controller_raid.json
│   ├── drive_0.json
│   ├── drive_1.json
│   ├── drive_2.json
│   ├── drive_3.json
│   ├── memory_collection.json
│   ├── memory_dimm_a1.json
│   ├── memory_dimm_a2.json
│   ├── ... (through a8, b1-b8)
│   ├── memory_metrics_a1.json
│   ├── memory_metrics_a2.json
│   ├── ... (through a8, b1-b8)
│   └── sel_entries.json
├── dell-r760/
│   ├── (same structure as dell-r750, different values)
│   └── ...
├── hpe-dl360-gen10/
│   ├── service_root.json
│   ├── manager.json
│   ├── system.json
│   ├── thermal.json
│   ├── power.json
│   ├── storage_collection.json
│   ├── storage_controller.json
│   ├── drive_0.json
│   ├── ... (drives)
│   ├── memory_collection.json
│   ├── memory_proc1dimm1.json
│   ├── ... (DIMMs)
│   ├── memory_metrics_proc1dimm1.json
│   ├── ... (metrics)
│   ├── iml_entries.json
│   ├── smartstorage_controllers.json
│   ├── smartstorage_controller_0.json
│   ├── smartstorage_drives_collection.json
│   ├── smartstorage_drive_0.json
│   └── smartstorage_drive_1.json
├── hpe-dl380-gen11/
│   ├── (same as hpe-dl360-gen10 WITHOUT smartstorage_* files)
│   └── ...
└── _templates/
    └── (optional: Jinja2 or string-format templates for generating per-DIMM/per-drive fixtures)
```

### 5.2 Fixture File Content

Each fixture file contains a complete Redfish JSON response body. The file must be valid JSON that the agent's normalization layer can parse without modification.

**Example: `dell-r750/service_root.json`**

```json
{
    "@odata.id": "/redfish/v1/",
    "@odata.type": "#ServiceRoot.v1_11_0.ServiceRoot",
    "Id": "RootService",
    "Name": "Root Service",
    "RedfishVersion": "1.17.0",
    "UUID": "4c4c4544-0033-4210-804e-c2c04f395332",
    "Systems": {
        "@odata.id": "/redfish/v1/Systems"
    },
    "Chassis": {
        "@odata.id": "/redfish/v1/Chassis"
    },
    "Managers": {
        "@odata.id": "/redfish/v1/Managers"
    },
    "SessionService": {
        "@odata.id": "/redfish/v1/SessionService"
    },
    "Oem": {
        "Dell": {
            "@odata.type": "#DellServiceRoot.v1_0_0.ServiceRootSummary",
            "ServiceTag": "HIQMOCK1"
        }
    }
}
```

**Example: `dell-r750/thermal.json` (truncated to show structure)**

```json
{
    "@odata.id": "/redfish/v1/Chassis/System.Embedded.1/Thermal",
    "@odata.type": "#Thermal.v1_7_0.Thermal",
    "Id": "Thermal",
    "Name": "Thermal",
    "Fans": [
        {
            "@odata.id": "/redfish/v1/Chassis/System.Embedded.1/Thermal#/Fans/0",
            "MemberId": "0",
            "Name": "System Board Fan1A",
            "Reading": 9800,
            "ReadingUnits": "RPM",
            "Status": {
                "Health": "OK",
                "State": "Enabled"
            },
            "LowerThresholdCritical": 480,
            "UpperThresholdCritical": null,
            "PhysicalContext": "SystemBoard",
            "Oem": {
                "Dell": {
                    "@odata.type": "#DellFan.v1_0_0.DellFan",
                    "FanPWM": 38,
                    "HardwareType": "System Board Fan"
                }
            }
        }
    ],
    "Temperatures": [
        {
            "@odata.id": "/redfish/v1/Chassis/System.Embedded.1/Thermal#/Temperatures/0",
            "MemberId": "0",
            "Name": "System Board Inlet Temp",
            "ReadingCelsius": 22,
            "Status": {
                "Health": "OK",
                "State": "Enabled"
            },
            "UpperThresholdNonCritical": 42,
            "UpperThresholdCritical": 47,
            "UpperThresholdFatal": null,
            "LowerThresholdNonCritical": 3,
            "LowerThresholdCritical": -7,
            "PhysicalContext": "SystemBoard"
        }
    ],
    "Redundancy": [
        {
            "@odata.id": "/redfish/v1/Chassis/System.Embedded.1/Thermal#/Redundancy/0",
            "MemberId": "0",
            "Name": "System Board Fan Redundancy",
            "Mode": "N+1",
            "Status": {
                "Health": "OK",
                "State": "Enabled"
            },
            "MinNumNeeded": 6,
            "MaxNumSupported": 8,
            "RedundancySet": [
                {"@odata.id": "/redfish/v1/Chassis/System.Embedded.1/Thermal#/Fans/0"},
                {"@odata.id": "/redfish/v1/Chassis/System.Embedded.1/Thermal#/Fans/1"}
            ]
        }
    ]
}
```

### 5.3 Cross-Reference Links

Fixture files reference each other via `@odata.id` links. The simulator does NOT follow these links automatically -- each link must correspond to a registered route.

**Link consistency rules:**

1. Every `@odata.id` value in a fixture file MUST have a corresponding route in the routing table.
2. Collection fixtures (e.g., `storage_collection.json`) list members with `@odata.id` links that point to individual resource routes.
3. The `_templates/` directory can contain Jinja2 templates for generating per-instance fixtures (e.g., 16 DIMMs from a single template), but the generated output must be valid standalone JSON.

### 5.4 Healthy Baseline Values

All fixture files represent a healthy server at steady state. The demo scenario (Doc 9, Section 2.2) defines the initial values:

| Component | Healthy Values |
|-----------|---------------|
| Fans | 8 fans, all `Health: OK`, `State: Enabled`, 9200-10400 RPM |
| Disks | 4 SSDs, all `Health: OK`, `PredictedMediaLifeLeftPercent` 85-98% |
| Memory | 16x 32GB DIMMs, `Health: OK`, `State: Enabled`, 0 ECC errors |
| PSUs | 2x 1400W, `Health: OK`, `State: Enabled`, redundancy OK, 186W system draw |
| Thermal | Inlet 22C, CPU1 54C, CPU2 52C, Exhaust 38C -- all within thresholds |
| Event logs | Empty (no SEL/IML entries) |

### 5.5 Vendor-Specific Differences in Fixtures

The Dell and HPE fixture files differ in:

| Property | Dell (R750/R760) | HPE (DL360/DL380) |
|----------|-----------------|-------------------|
| Fan names | `"System Board Fan1A"` through `"System Board Fan4B"` | `"Fan 1"` through `"Fan 8"` |
| Fan OEM data | `Oem.Dell.DellFan.FanPWM` | `Oem.Hpe.Location`, `Oem.Hpe.HotPluggable` |
| Temperature sensor names | `"System Board Inlet Temp"`, `"CPU1 Temp"` | `"01-Inlet Ambient"`, `"02-CPU 1"` |
| DIMM IDs | `DIMM.Socket.A1` | `proc1dimm1` |
| DIMM OEM data | `Oem.Dell.DellMemory.BankLabel` | `Oem.Hpe.DIMMStatus` |
| Drive OEM data | `Oem.Dell.DellPhysicalDisk.RaidStatus` | `Oem.Hpe.CurrentTemperatureCelsius` |
| PSU OEM data | `Oem.Dell.DellPowerSupply.DetailedState` | `Oem.Hpe.BayNumber`, `Oem.Hpe.Mismatched` |
| Manager model | `"iDRAC9"` or `"iDRAC10"` | `"iLO 5"` or `"iLO 6"` |
| ServiceRoot OEM | `Oem.Dell.ServiceTag` | `Oem.Hpe` with firmware info |
| Memory type | DDR4 (R750), DDR5 (R760) | DDR4 (DL360 Gen10), DDR5 (DL380 Gen11) |
| Event log path | `Sel/Entries` | `IML/Entries` |

The dell-r750 and dell-r760 profiles share the same structure but differ in:
- Manager model (`iDRAC9` vs `iDRAC10`)
- Memory type (DDR4 vs DDR5)
- RedfishVersion (1.17.0 vs 1.19.0)
- Firmware versions

The hpe-dl360-gen10 and hpe-dl380-gen11 profiles differ in:
- Manager model (`iLO 5` vs `iLO 6`)
- Memory type (DDR4 vs DDR5)
- SmartStorage endpoints (present on iLO5, absent on iLO6)
- Storage controller IDs (`DE009000` on iLO5 vs `DE00A000` on iLO6)

---

## 6. State Management

### 6.1 State Lifecycle

```
                     Startup
                       │
                       ▼
              ┌─────────────────┐
              │  Load Fixtures  │
              │  from disk      │
              └────────┬────────┘
                       │
                       ▼
              ┌─────────────────┐
              │  Deep copy into │◄──── POST /test/reset
              │  in-memory      │
              │  device state   │
              └────────┬────────┘
                       │
                       ▼
              ┌─────────────────┐
              │  Serving        │
              │  requests       │
              │  from state     │◄──── POST /test/inject-fault
              └────────┬────────┘      POST /test/inject-gradual
                       │               POST /test/inject-log
                       ▼
              ┌─────────────────┐
              │  Shutdown       │
              │  (state lost)   │
              └─────────────────┘
```

### 6.2 State Structure

Each device maintains its own independent state object:

```python
@dataclass
class DeviceState:
    device_profile: str                  # e.g., "dell-r750"
    service_root: dict                   # ServiceRoot response
    manager: dict                        # Manager response
    system: dict                         # System response
    thermal: dict                        # Thermal response (Fans + Temperatures + Redundancy)
    power: dict                          # Power response (PowerSupplies + PowerControl + Redundancy)
    storage_collection: dict             # Storage collection response
    storage_controllers: dict[str, dict] # Controller ID -> controller response
    drives: dict[str, dict]              # Drive path -> drive response
    memory_collection: dict              # Memory collection response
    memory_dimms: dict[str, dict]        # DIMM ID -> DIMM response
    memory_metrics: dict[str, dict]      # DIMM ID -> MemoryMetrics response
    log_entries: list[dict]              # Event log entries (mutable, new entries prepended)
    smartstorage_controllers: dict | None  # iLO5 only
    smartstorage_drives: dict[str, dict] | None  # iLO5 only
    active_gradual_faults: list[GradualFault]  # Currently running gradual faults
```

### 6.3 State Initialization

At startup, the fixture loader:

1. Reads all JSON fixture files for the device profile from `harkeniq/mock/fixtures/{device_profile}/`.
2. Creates a deep copy of each parsed JSON object.
3. Populates the `DeviceState` fields.
4. Registers routes that reference the state object.

### 6.4 Immutability of Fixtures on Disk

Fault injection ONLY modifies the in-memory `DeviceState`. The fixture files on disk are NEVER written to. This ensures:
- Resetting to healthy state is a simple deep copy from the original fixture data.
- Multiple test runs are isolated.
- Fixture files remain a clean reference for what "healthy" looks like.

### 6.5 Concurrent Access

The simulator runs on a single asyncio event loop. All state mutations (fault injection, gradual fault ticks, log entry additions) and state reads (Redfish GET handlers) execute as coroutines on the same event loop. No locks are required because asyncio is cooperative and single-threaded.

---

## 7. Fault Injection API

All fault injection endpoints are prefixed with `/test/` to distinguish them from Redfish endpoints. These endpoints are NOT authenticated (no X-Auth-Token required) regardless of the `--no-auth` flag.

### 7.1 POST /test/inject-fault

Inject an instantaneous fault into a simulated device.

**Request:**

```json
{
    "device": "dell-r750",
    "fault_type": "fan",
    "target": "Fan1A",
    "params": {
        "health": "Critical",
        "speed_rpm": 0
    }
}
```

**Response (200 OK):**

```json
{
    "status": "injected",
    "device": "dell-r750",
    "fault_type": "fan",
    "target": "Fan1A",
    "mutations_applied": [
        "Fans[0].Status.Health: OK -> Critical",
        "Fans[0].Reading: 9800 -> 0"
    ]
}
```

**Response (400 Bad Request):**

```json
{
    "status": "error",
    "message": "Unknown target 'Fan99' for fault_type 'fan' on device 'dell-r750'"
}
```

#### 7.1.1 Target Resolution

The `target` field identifies which component to affect. The simulator resolves target names to array indices within the fixture data:

| Fault Type | Target Format (Dell) | Target Format (HPE) | Resolution |
|------------|---------------------|---------------------|------------|
| fan | `Fan1A`, `Fan1B`, ..., `Fan4B` | `Fan 1`, `Fan 2`, ..., `Fan 8` | Matches `Fans[].Name` containing the target string |
| disk | `Disk.Bay.0`, `Disk.Bay.1`, ... | `Drive 0`, `Drive 1`, ... | Matches drive path or `Name` |
| memory | `DIMM.Socket.A1`, ... | `proc1dimm1`, ... | Matches DIMM ID |
| psu | `PS1`, `PS2` | `PS1`, `PS2` | Matches by MemberId ("0" = PS1, "1" = PS2) |
| thermal | `Inlet`, `CPU1`, `CPU2`, `Exhaust` | `Inlet Ambient`, `CPU 1`, `CPU 2` | Matches `Temperatures[].Name` containing the target string |

#### 7.1.2 Fault Type: fan

Modifies the `thermal.json` in-memory state (Fans array and Redundancy array).

**Params schema:**

| Param | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `health` | string | no | (unchanged) | `"OK"`, `"Warning"`, `"Critical"` -- sets `Fans[n].Status.Health` |
| `state` | string | no | (unchanged) | `"Enabled"`, `"Absent"` -- sets `Fans[n].Status.State` |
| `speed_rpm` | integer | no | (unchanged) | Sets `Fans[n].Reading` to this value |
| `speed_pct` | integer | no | (unchanged) | Sets `Fans[n].Oem.Dell.DellFan.FanPWM` (Dell only) |
| `redundancy_health` | string | no | (unchanged) | Sets `Redundancy[0].Status.Health` |
| `redundancy_state` | string | no | (unchanged) | Sets `Redundancy[0].Status.State` |

**Mutations applied to state:**

1. Find the fan in `thermal["Fans"]` whose `Name` contains the target string.
2. If `health` provided: set `fan["Status"]["Health"]` to the value.
3. If `state` provided: set `fan["Status"]["State"]` to the value.
4. If `speed_rpm` provided: set `fan["Reading"]` to the value.
5. If `speed_pct` provided (Dell only): set `fan["Oem"]["Dell"]["DellFan"]["FanPWM"]` to the value.
6. If `redundancy_health` provided: set `thermal["Redundancy"][0]["Status"]["Health"]` to the value.
7. If `redundancy_state` provided: set `thermal["Redundancy"][0]["Status"]["State"]` to the value.
8. If health is `Critical` or state is `Absent`: also update `system["Status"]["HealthRollup"]` to `"Warning"` or `"Critical"` as appropriate.

#### 7.1.3 Fault Type: disk

Modifies the individual drive response in `drives[target_path]`.

**Params schema:**

| Param | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `health` | string | no | (unchanged) | `"OK"`, `"Warning"`, `"Critical"` -- sets `Status.Health` |
| `life_left_pct` | integer | no | (unchanged) | Sets `PredictedMediaLifeLeftPercent` (0-100). On iLO5 SmartStorage, sets `SSDEnduranceUtilizationPercentage` to `100 - life_left_pct`. |
| `smart_alert` | boolean | no | (unchanged) | Sets `FailurePredicted`. On Dell, also sets `Oem.Dell.DellPhysicalDisk.SmartAlertIndication` to `"Yes"` or `"No"`. |
| `raid_status` | string | no | (unchanged) | Dell only. Sets `Oem.Dell.DellPhysicalDisk.RaidStatus`. Valid: `"Online"`, `"Degraded"`, `"Failed"`, `"Rebuilding"`, `"Ready"`, `"Foreign"`, `"Offline"`. |
| `temperature_c` | integer | no | (unchanged) | HPE only. Sets `Oem.Hpe.CurrentTemperatureCelsius`. |

**Mutations applied to state:**

1. Resolve `target` to a drive key in `drives` dict.
2. Apply each provided param to the corresponding field in the drive dict.
3. If `health` is `Critical`: also update `system["Status"]["HealthRollup"]`.

#### 7.1.4 Fault Type: memory

Modifies the individual DIMM response in `memory_dimms[target]` and its metrics in `memory_metrics[target]`.

**Params schema:**

| Param | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `health` | string | no | (unchanged) | `"OK"`, `"Warning"`, `"Critical"` -- sets DIMM `Status.Health` |
| `state` | string | no | (unchanged) | `"Enabled"`, `"Absent"` -- sets DIMM `Status.State` |
| `alarm_ecc_correctable` | boolean | no | (unchanged) | Sets `HealthData.AlarmTrips.CorrectableECCError` in MemoryMetrics |
| `alarm_ecc_uncorrectable` | boolean | no | (unchanged) | Sets `HealthData.AlarmTrips.UncorrectableECCError` in MemoryMetrics |
| `alarm_temperature` | boolean | no | (unchanged) | Sets `HealthData.AlarmTrips.Temperature` in MemoryMetrics |
| `ecc_correctable_lifetime` | integer | no | (unchanged) | Sets `LifeTime.CorrectableECCErrorCount` in MemoryMetrics |
| `ecc_uncorrectable_lifetime` | integer | no | (unchanged) | Sets `LifeTime.UncorrectableECCErrorCount` in MemoryMetrics |
| `ecc_correctable_current` | integer | no | (unchanged) | Sets `CurrentPeriod.CorrectableECCErrorCount` in MemoryMetrics |
| `ecc_uncorrectable_current` | integer | no | (unchanged) | Sets `CurrentPeriod.UncorrectableECCErrorCount` in MemoryMetrics |

**Mutations applied to state:**

1. Resolve `target` to a DIMM key in `memory_dimms` dict.
2. Apply `health` and `state` to the DIMM dict.
3. Apply alarm and ECC params to the corresponding MemoryMetrics dict (`memory_metrics[target]`).
4. If `health` is `Critical` or `alarm_ecc_uncorrectable` is `true`: update `system["MemorySummary"]["Status"]["HealthRollup"]` to `"Critical"`.

#### 7.1.5 Fault Type: psu

Modifies the `power.json` in-memory state (PowerSupplies array and Redundancy array).

**Params schema:**

| Param | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `health` | string | no | (unchanged) | `"OK"`, `"Warning"`, `"Critical"` -- sets `PowerSupplies[n].Status.Health` |
| `state` | string | no | (unchanged) | `"Enabled"`, `"Absent"` -- sets `PowerSupplies[n].Status.State` |
| `redundancy_health` | string | no | (unchanged) | Sets `Redundancy[0].Status.Health` (e.g., `"Warning"` for degraded) |
| `redundancy_state` | string | no | (unchanged) | Sets `Redundancy[0].Status.State` |
| `input_voltage` | integer | no | (unchanged) | Sets `PowerSupplies[n].LineInputVoltage` |
| `output_watts` | integer | no | (unchanged) | Sets `PowerSupplies[n].LastPowerOutputWatts` |
| `capacity_watts` | integer | no | (unchanged) | Sets `PowerSupplies[n].PowerCapacityWatts` |

**Mutations applied to state:**

1. Resolve `target` (PS1 or PS2) to `PowerSupplies` array index.
2. Apply each provided param.
3. If `state` is `Absent`: also set `health` to `"Critical"`, set `LastPowerOutputWatts` to 0, set `LineInputVoltage` to 0.
4. If `redundancy_health` provided: set `Redundancy[0].Status.Health`.
5. If a PSU goes absent: update `PowerControl[0].PowerConsumedWatts` to reflect full load on the remaining PSU (no change in total draw, but only one PSU carries it).
6. Update `system["Status"]["HealthRollup"]` if any PSU is Critical.

#### 7.1.6 Fault Type: thermal

Modifies the `thermal.json` in-memory state (Temperatures array).

**Params schema:**

| Param | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `reading_c` | float | no | (unchanged) | Sets `Temperatures[n].ReadingCelsius` for the target sensor |
| `health` | string | no | (unchanged) | Sets `Temperatures[n].Status.Health` |
| `cpu1_reading_c` | float | no | (unchanged) | Convenience: sets CPU1 sensor `ReadingCelsius` (for cascade scenarios) |
| `cpu2_reading_c` | float | no | (unchanged) | Convenience: sets CPU2 sensor `ReadingCelsius` |
| `exhaust_reading_c` | float | no | (unchanged) | Convenience: sets Exhaust sensor `ReadingCelsius` |

**Mutations applied to state:**

1. Resolve `target` to a temperature sensor in `thermal["Temperatures"]` by name match.
2. If `reading_c` provided: set `Temperatures[n]["ReadingCelsius"]`.
3. If `health` provided: set `Temperatures[n]["Status"]["Health"]`.
4. If the `reading_c` exceeds `UpperThresholdNonCritical`: auto-set `health` to `"Warning"`.
5. If the `reading_c` exceeds `UpperThresholdCritical`: auto-set `health` to `"Critical"`.
6. Apply convenience params (`cpu1_reading_c`, etc.) to their respective sensors.
7. Update `system["Status"]["HealthRollup"]` if any thermal sensor is Critical.

### 7.2 POST /test/inject-gradual

Inject a gradually changing value. The simulator creates an asyncio task that updates the target field at regular intervals until the duration expires.

**Request:**

```json
{
    "device": "dell-r750",
    "fault_type": "fan",
    "target": "Fan1A",
    "params": {
        "field": "speed_rpm",
        "start_value": 9800,
        "end_value": null,
        "rate_per_second": -3.33,
        "duration_seconds": 60,
        "update_interval_seconds": 2.0
    }
}
```

**Params schema:**

| Param | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `field` | string | yes | -- | Which field to change. Must be a valid param name for the fault type (e.g., `speed_rpm`, `reading_c`, `life_left_pct`, `ecc_correctable_lifetime`). |
| `start_value` | float | yes | -- | Value at t=0. Immediately applied. |
| `end_value` | float | no | `null` | If provided, the value stops changing when it reaches this bound. |
| `rate_per_second` | float | yes | -- | Rate of change per second. Negative for declining values (e.g., -3.33 RPM/s = -200 RPM/min). Positive for increasing values (e.g., ECC counts). |
| `duration_seconds` | float | yes | -- | How long the gradual change runs. The task cancels after this many seconds. |
| `update_interval_seconds` | float | no | `1.0` | How often the state is updated. 1.0 means once per second. For the demo, 2.0 is typical (matches poll interval compression). |

**Response (200 OK):**

```json
{
    "status": "started",
    "gradual_fault_id": "gf-001",
    "device": "dell-r750",
    "fault_type": "fan",
    "target": "Fan1A",
    "field": "speed_rpm",
    "start_value": 9800,
    "rate_per_second": -3.33,
    "projected_final_value": 9600.2,
    "duration_seconds": 60
}
```

**Implementation:**

```python
async def _gradual_fault_task(self, fault: GradualFault):
    """Background task that mutates device state over time."""
    elapsed = 0.0
    current = fault.start_value
    while elapsed < fault.duration_seconds:
        # Apply current value using the same mutation logic as inject-fault
        self._apply_param(fault.device, fault.fault_type, fault.target,
                          fault.field, current)
        await asyncio.sleep(fault.update_interval_seconds)
        elapsed += fault.update_interval_seconds
        current += fault.rate_per_second * fault.update_interval_seconds
        # Clamp to end_value if specified
        if fault.end_value is not None:
            if fault.rate_per_second < 0:
                current = max(current, fault.end_value)
            else:
                current = min(current, fault.end_value)
```

**Cancellation:** A gradual fault can be canceled by:
- `POST /test/reset` for the device (cancels all gradual faults).
- The duration expiring.
- A new `inject-fault` or `inject-gradual` targeting the same device + fault_type + target + field (replaces the existing one).

### 7.3 POST /test/reset

Reset a device (or all devices) to the healthy baseline state loaded from fixture files.

**Request:**

```json
{"device": "dell-r750"}
```

Or:

```json
{"device": "all"}
```

**Response (200 OK):**

```json
{
    "status": "reset",
    "devices_reset": ["dell-r750"],
    "gradual_faults_canceled": 1,
    "log_entries_cleared": 3
}
```

**Implementation:**

1. Cancel all active gradual fault tasks for the device.
2. Deep copy the original fixture data into the device state (overwriting all mutations).
3. Clear all injected log entries.
4. Reset session state (all sessions remain valid).

### 7.4 GET /test/state

Return the current state of all simulated devices as a summary. Useful for debugging and test assertions.

**Response (200 OK):**

```json
{
    "devices": {
        "dell-r750": {
            "fans": [
                {"name": "System Board Fan1A", "reading_rpm": 9800, "health": "OK"},
                {"name": "System Board Fan1B", "reading_rpm": 10200, "health": "OK"}
            ],
            "temperatures": [
                {"name": "System Board Inlet Temp", "reading_c": 22, "health": "OK"},
                {"name": "CPU1 Temp", "reading_c": 54, "health": "OK"}
            ],
            "disks": [
                {"name": "Disk.Bay.0", "health": "OK", "life_left_pct": 98},
                {"name": "Disk.Bay.1", "health": "OK", "life_left_pct": 92}
            ],
            "memory": [
                {"name": "DIMM.Socket.A1", "health": "OK", "ecc_correctable": 0, "ecc_uncorrectable": 0}
            ],
            "psus": [
                {"name": "PS1 Status", "health": "OK", "state": "Enabled", "output_watts": 93},
                {"name": "PS2 Status", "health": "OK", "state": "Enabled", "output_watts": 93}
            ],
            "system_health_rollup": "OK",
            "active_gradual_faults": 0,
            "log_entry_count": 0
        }
    }
}
```

### 7.5 POST /test/inject-log

Add an event log entry to the device's SEL (Dell) or IML (HPE) log.

**Request:**

```json
{
    "device": "dell-r750",
    "severity": "Critical",
    "message": "Fan1A has failed",
    "message_id": "FAN0001",
    "component_fqdd": "Fan.Embedded.1A",
    "category": "System Health"
}
```

**Params schema:**

| Param | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `device` | string | yes | -- | Device profile |
| `severity` | string | yes | -- | `"OK"`, `"Warning"`, `"Critical"` |
| `message` | string | yes | -- | Human-readable event message |
| `message_id` | string | yes | -- | Structured message code (e.g., `"FAN0001"`, `"PSU0002"`) |
| `component_fqdd` | string | no | `null` | Dell only: FQDD of the affected component (e.g., `"Fan.Embedded.1A"`, `"Disk.Bay.2:..."`) |
| `category` | string | no | `"System Health"` | Dell: maps to `Oem.Dell.DellSELEntry.Category`. HPE: maps to `Oem.Hpe.Categories[0]`. |

**Response (200 OK):**

```json
{
    "status": "injected",
    "entry_id": "42",
    "device": "dell-r750",
    "timestamp": "2026-08-18T14:30:00Z"
}
```

**Mutation:**

A new entry is prepended to the device's `log_entries` list. The entry is formatted as a valid Redfish log entry with all standard and OEM fields:

Dell example:
```json
{
    "Id": "42",
    "Created": "2026-08-18T14:30:00Z",
    "Severity": "Critical",
    "Message": "Fan1A has failed",
    "MessageId": "FAN0001",
    "EntryType": "SEL",
    "Oem": {
        "Dell": {
            "DellSELEntry": {
                "Category": "System Health",
                "FQDD": "Fan.Embedded.1A",
                "DeviceType": "Fan"
            }
        }
    }
}
```

HPE example:
```json
{
    "Id": "42",
    "Created": "2026-08-18T14:30:00Z",
    "Severity": "Critical",
    "Message": "Fan1A has failed",
    "MessageId": "FAN0001",
    "EntryType": "Oem",
    "Oem": {
        "Hpe": {
            "Class": 17,
            "Code": 1,
            "Categories": ["Hardware", "Cooling"],
            "Count": 1,
            "Repaired": false
        }
    }
}
```

The `DeviceType` and HPE `Class`/`Code` values are derived from the `fault_type` or `category`:

| Category | Dell DeviceType | HPE Class |
|----------|----------------|-----------|
| Fan / Cooling | `"Fan"` | 17 |
| Disk / Storage | `"Disk"` | 2 |
| Memory | `"Memory"` | 7 |
| PSU / Power | `"PSU"` | 10 |
| Thermal | `"Temperature"` | 17 |

### 7.6 POST /test/inject-fault Automatic Log Entry

When a fault is injected via `/test/inject-fault` and the resulting health is `Warning` or `Critical`, the simulator ALSO automatically creates a corresponding log entry (as if the BMC generated it). This mirrors real BMC behavior and ensures the agent's log polling corroborates the sensor data.

This automatic log entry can be suppressed by including `"auto_log": false` in the fault injection params.

---

## 8. Session Authentication

### 8.1 Session Creation

**Request:**

```
POST /redfish/v1/SessionService/Sessions
Content-Type: application/json

{
    "UserName": "admin",
    "Password": "password"
}
```

**Response (201 Created):**

Headers:
```
X-Auth-Token: ses-a1b2c3d4e5f6
Location: /redfish/v1/SessionService/Sessions/ses-a1b2c3d4e5f6
```

Body:
```json
{
    "@odata.id": "/redfish/v1/SessionService/Sessions/ses-a1b2c3d4e5f6",
    "@odata.type": "#Session.v1_3_0.Session",
    "Id": "ses-a1b2c3d4e5f6",
    "Name": "User Session",
    "UserName": "admin"
}
```

**Error (401 Unauthorized):**

```json
{
    "error": {
        "@Message.ExtendedInfo": [
            {
                "Message": "The authentication credentials included with this request are missing or invalid.",
                "MessageId": "Base.1.0.GeneralError",
                "Severity": "Critical"
            }
        ]
    }
}
```

### 8.2 Session Validation

When `--no-auth` is NOT set, every Redfish GET/POST/DELETE request (except `POST /redfish/v1/SessionService/Sessions`) must include:

```
X-Auth-Token: ses-a1b2c3d4e5f6
```

The simulator validates that the token exists in its active sessions set. Invalid or missing tokens return 401.

### 8.3 Session Deletion

**Request:**

```
DELETE /redfish/v1/SessionService/Sessions/ses-a1b2c3d4e5f6
X-Auth-Token: ses-a1b2c3d4e5f6
```

**Response (200 OK):**

```json
{
    "status": "deleted"
}
```

### 8.4 Session Behavior

| Behavior | Value |
|----------|-------|
| Token format | `ses-` prefix + 12 hex characters (random) |
| Token lifetime | 30 minutes (matching typical BMC session timeout) |
| Max concurrent sessions | 8 per device (matching iDRAC limit) |
| Expired session response | 401 with message "Session has expired" |
| Max sessions exceeded | 503 with message "Maximum number of sessions reached" |

### 8.5 --no-auth Mode

When `--no-auth` is set:
- The `POST /redfish/v1/SessionService/Sessions` endpoint still works (for testing session creation itself), but its token is not required on subsequent requests.
- All Redfish GET requests are served without authentication.
- This mode is the default for programmatic test usage (`MockSimulator(no_auth=True)`).

### 8.6 Default Credentials

| Parameter | Default | Configurable via |
|-----------|---------|-----------------|
| Username | `admin` | `--credentials` CLI flag, `MockSimulator(credentials=(...))` |
| Password | `password` | Same |

These are intentionally simple defaults. They do NOT represent production BMC credentials.

---

## 9. Error Simulation

The simulator can simulate common BMC error behaviors to test the agent's retry logic, error handling, and rate limiting.

### 9.1 Configuration Endpoint

**POST /test/config**

```json
{
    "device": "dell-r750",
    "latency_ms": 500,
    "error_rate": 0.05,
    "rate_limit_rps": 2
}
```

All config parameters are optional. Omitted parameters retain their current value.

### 9.2 Response Latency

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `latency_ms` | integer | `0` | Artificial delay added to every Redfish response (not /test/ endpoints). Simulates slow BMC response times. |
| `latency_jitter_ms` | integer | `0` | Random jitter added to latency. Actual delay is `latency_ms + random(0, latency_jitter_ms)`. |

**Implementation:** Before returning the Redfish response, the handler calls `await asyncio.sleep(delay / 1000.0)`.

### 9.3 Error Rate

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `error_rate` | float | `0.0` | Fraction of Redfish requests that return 503 Service Unavailable (0.0 to 1.0). |

**Implementation:** Before processing a Redfish request, roll `random.random()`. If below `error_rate`, return 503 with:

```json
{
    "error": {
        "@Message.ExtendedInfo": [
            {
                "Message": "The service is temporarily unavailable. Retry after the duration specified in the Retry-After header.",
                "MessageId": "Base.1.0.ServiceTemporarilyUnavailable",
                "Severity": "Critical"
            }
        ]
    }
}
```

Headers include `Retry-After: 15` (matching Dell's typical Retry-After value).

The agent's Redfish client must handle this by respecting `Retry-After` and retrying (Doc 5, Section 1.3).

### 9.4 Rate Limiting

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `rate_limit_rps` | float | `0` (disabled) | Maximum Redfish requests per second per device. Excess requests return 429 Too Many Requests. |

**Implementation:** Token bucket rate limiter per device. When the bucket is empty, return 429 with:

```json
{
    "error": {
        "@Message.ExtendedInfo": [
            {
                "Message": "The number of requests per time period has been exceeded.",
                "MessageId": "Base.1.0.GeneralError",
                "Severity": "Warning"
            }
        ]
    }
}
```

Headers include `Retry-After: 1`.

### 9.5 Reset Error Simulation

```json
{
    "device": "dell-r750",
    "latency_ms": 0,
    "error_rate": 0.0,
    "rate_limit_rps": 0
}
```

Or use `POST /test/reset` (which also resets error simulation config to defaults).

---

## 10. Multi-Device Mode

### 10.1 Architecture

When `--device all` is specified, the simulator starts 4 independent aiohttp applications, each on its own port:

```
┌─────────────────────────────────────────────────┐
│              Mock Simulator Process              │
│                                                  │
│  ┌─────────────┐  ┌─────────────┐               │
│  │ dell-r750   │  │ dell-r760   │               │
│  │ :8443       │  │ :8444       │               │
│  │ own state   │  │ own state   │               │
│  └─────────────┘  └─────────────┘               │
│                                                  │
│  ┌─────────────┐  ┌─────────────┐               │
│  │ hpe-dl360   │  │ hpe-dl380   │               │
│  │ gen10 :8445 │  │ gen11 :8446 │               │
│  │ own state   │  │ own state   │               │
│  └─────────────┘  └─────────────┘               │
│                                                  │
│  ┌─────────────────────────────────────────────┐ │
│  │  Shared /test/ control plane (:8440)        │ │
│  │  All fault injection goes here.             │ │
│  │  Routes to correct device by "device" field.│ │
│  └─────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────┘
```

### 10.2 Control Plane

In multi-device mode, a separate control listener on port 8440 handles all `/test/` endpoints. The `device` field in each request body routes the command to the correct device's state.

In single-device mode, the `/test/` endpoints are served on the same port as the Redfish endpoints (e.g., 8443).

### 10.3 Device Independence

Each device has:
- Its own `DeviceState` object.
- Its own session table (tokens are not shared across devices).
- Its own error simulation config (latency, error rate, rate limit).
- Its own gradual fault tasks.

Fault injection targeting one device does NOT affect others.

### 10.4 Port Assignment

| Port | Purpose |
|------|---------|
| 8440 | Control plane (`/test/` endpoints) -- multi-device mode only |
| 8443 | Dell R750 (iDRAC9) Redfish endpoints |
| 8444 | Dell R760 (iDRAC10) Redfish endpoints |
| 8445 | HPE DL360 Gen10 (iLO5) Redfish endpoints |
| 8446 | HPE DL380 Gen11 (iLO6) Redfish endpoints |

The base port is configurable with `--port`. If `--port 9000` is specified:
- Control plane: 8440 (always fixed, or `--port - 3` if base port is not 8443)
- Device 1: 9000
- Device 2: 9001
- Device 3: 9002
- Device 4: 9003

---

## 11. Peer Simulation

The mock simulator can simulate peer agents that send UDP heartbeat packets, enabling testing of the heartbeat tracker and witness model without running multiple agent instances.

### 11.1 POST /test/peer/start

Start a simulated peer that sends UDP heartbeat packets.

**Request:**

```json
{
    "peer_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "name": "rack-12-server-03",
    "host": "127.0.0.1",
    "port": 5150,
    "interval_seconds": 1.0,
    "hmac_secret": "demo-secret-key"
}
```

**Params schema:**

| Param | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `peer_id` | string (UUID) | yes | -- | UUID identifying this simulated peer |
| `name` | string | yes | -- | Human-readable peer name (e.g., `"rack-12-server-03"`) |
| `host` | string | no | `"127.0.0.1"` | Address to send heartbeat packets to |
| `port` | integer | no | `5150` | UDP port to send heartbeat packets to |
| `interval_seconds` | float | no | `1.0` | Interval between heartbeat packets |
| `hmac_secret` | string | no | `"demo-secret-key"` | HMAC-SHA256 shared secret for signing heartbeat packets |

**Response (200 OK):**

```json
{
    "status": "started",
    "peer_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "name": "rack-12-server-03",
    "sending_to": "127.0.0.1:5150",
    "interval_seconds": 1.0
}
```

**Implementation:**

The simulator creates an asyncio task that sends UDP heartbeat packets at the configured interval. Each heartbeat packet is formatted identically to the agent's heartbeat protocol (Doc 6):

```python
heartbeat_payload = {
    "peer_id": peer_id,
    "name": name,
    "timestamp": time.time(),
    "seq": sequence_number,
    "health_summary": {
        "fan": "OK",
        "disk": "OK",
        "memory": "OK",
        "psu": "OK",
        "thermal": "OK"
    }
}
# HMAC-SHA256 signature appended
```

### 11.2 POST /test/peer/stop

Stop a simulated peer. It immediately stops sending heartbeat packets, which causes the real agent to detect the peer as unresponsive after 3 missed heartbeats.

**Request:**

```json
{
    "peer_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
}
```

**Response (200 OK):**

```json
{
    "status": "stopped",
    "peer_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "name": "rack-12-server-03",
    "heartbeats_sent": 25
}
```

### 11.3 POST /test/peer/update

Update the health summary of a simulated peer. This changes what the peer reports in subsequent heartbeat packets, allowing the agent to see a peer's health change before it goes down (pre-failure evidence for the witness model).

**Request:**

```json
{
    "peer_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "health_summary": {
        "fan": "Warning",
        "disk": "OK",
        "memory": "OK",
        "psu": "OK",
        "thermal": "OK"
    }
}
```

**Response (200 OK):**

```json
{
    "status": "updated",
    "peer_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "health_summary": {"fan": "Warning", "disk": "OK", "memory": "OK", "psu": "OK", "thermal": "OK"}
}
```

### 11.4 GET /test/peer/status

List all simulated peers and their current state.

**Response (200 OK):**

```json
{
    "peers": [
        {
            "peer_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
            "name": "rack-12-server-03",
            "state": "sending",
            "heartbeats_sent": 25,
            "interval_seconds": 1.0,
            "health_summary": {"fan": "OK", "disk": "OK", "memory": "OK", "psu": "OK", "thermal": "OK"}
        },
        {
            "peer_id": "b2c3d4e5-f6a1-7890-abcd-ef1234567890",
            "name": "rack-12-server-05",
            "state": "sending",
            "heartbeats_sent": 25,
            "interval_seconds": 1.0,
            "health_summary": {"fan": "OK", "disk": "OK", "memory": "OK", "psu": "OK", "thermal": "OK"}
        }
    ]
}
```

### 11.5 Demo Peer Setup

The `harken demo` command (Doc 9) sets up peers as part of its initialization:

```python
# Demo pre-conditions (t < 0):
await sim_client.post("/test/peer/start", json={
    "peer_id": PEER_1_UUID,
    "name": "rack-12-server-03",
    "host": "127.0.0.1",
    "port": 5150,
    "interval_seconds": 1.0,
    "hmac_secret": DEMO_HMAC_SECRET
})
await sim_client.post("/test/peer/start", json={
    "peer_id": PEER_2_UUID,
    "name": "rack-12-server-05",
    "host": "127.0.0.1",
    "port": 5150,
    "interval_seconds": 1.0,
    "hmac_secret": DEMO_HMAC_SECRET
})

# At t=25: stop peer 1 heartbeats
await sim_client.post("/test/peer/stop", json={
    "peer_id": PEER_1_UUID
})
```

---

## 12. Response Headers

All Redfish responses include these headers to match real BMC behavior:

| Header | Value |
|--------|-------|
| `Content-Type` | `application/json;odata.metadata=minimal;charset=utf-8` |
| `OData-Version` | `4.0` |
| `Server` | `HarkenIQ-MockSimulator/1.0` (Dell returns `iDRAC/8`, HPE returns `iLO/5`) |
| `Cache-Control` | `no-cache` |

The `Server` header value depends on the device profile:

| Profile | Server Header |
|---------|--------------|
| `dell-r750` | `iDRAC/8` |
| `dell-r760` | `iDRAC/8` |
| `hpe-dl360-gen10` | `iLO/5` |
| `hpe-dl380-gen11` | `iLO/6` |

---

## 13. Logging

The simulator logs all requests and state mutations for debugging.

### 13.1 Request Logging

```
2026-08-18 14:30:00 INFO  [dell-r750:8443] GET /redfish/v1/Chassis/System.Embedded.1/Thermal -> 200 (12ms)
2026-08-18 14:30:01 INFO  [dell-r750:8443] GET /redfish/v1/Systems/System.Embedded.1 -> 200 (8ms)
2026-08-18 14:30:05 WARN  [dell-r750:8443] GET /redfish/v1/BadPath -> 404 (1ms)
```

### 13.2 Fault Injection Logging

```
2026-08-18 14:30:10 INFO  [dell-r750] FAULT INJECTED: fan Fan1A health=Critical speed_rpm=0
2026-08-18 14:30:10 INFO  [dell-r750] AUTO-LOG: Critical "Fan1A has failed" (FAN0001)
2026-08-18 14:30:10 INFO  [dell-r750] HEALTH ROLLUP: OK -> Critical
```

### 13.3 Gradual Fault Logging

```
2026-08-18 14:30:15 INFO  [dell-r750] GRADUAL START: fan Fan1A speed_rpm 9800 -> declining at -3.33/s for 60s
2026-08-18 14:30:17 DEBUG [dell-r750] GRADUAL TICK: fan Fan1A speed_rpm = 9793.34
2026-08-18 14:31:15 INFO  [dell-r750] GRADUAL END: fan Fan1A speed_rpm final=9600.2
```

### 13.4 Log Level

| Level | Content |
|-------|---------|
| `debug` | Every request, every gradual tick, state snapshots |
| `info` | Requests, fault injections, state changes |
| `warning` | 404s, auth failures, config issues |
| `error` | Server errors, fixture load failures |

---

## 14. Test Integration

### 14.1 pytest Fixture

The simulator provides a pytest fixture for integration tests:

```python
# conftest.py
import pytest
from harkeniq.mock.simulator import MockSimulator

@pytest.fixture
async def mock_bmc(unused_tcp_port):
    """Start a mock BMC simulator for the test."""
    sim = MockSimulator(
        device="dell-r750",
        port=unused_tcp_port,
        no_auth=True
    )
    await sim.start()
    yield sim
    await sim.stop()

@pytest.fixture
async def mock_bmc_all(unused_tcp_port_factory):
    """Start all 4 mock BMC simulators."""
    sims = []
    for device in ["dell-r750", "dell-r760", "hpe-dl360-gen10", "hpe-dl380-gen11"]:
        port = unused_tcp_port_factory()
        sim = MockSimulator(device=device, port=port, no_auth=True)
        await sim.start()
        sims.append(sim)
    yield sims
    for sim in sims:
        await sim.stop()
```

### 14.2 Test Assertions via /test/state

Tests can verify simulator state after fault injection:

```python
async def test_fan_fault_updates_state(mock_bmc):
    await mock_bmc.inject_fault("fan", target="Fan1A", params={"health": "Critical", "speed_rpm": 0})

    state = await mock_bmc.get_state()
    fan = next(f for f in state["fans"] if f["name"] == "System Board Fan1A")
    assert fan["health"] == "Critical"
    assert fan["reading_rpm"] == 0
```

### 14.3 22 Test Paths Coverage

The mock simulator supports all 22 integration test paths defined in Doc 12:

| Test Path | Device | Fault | Simulator Action |
|-----------|--------|-------|-----------------|
| 1 | dell-r750 | Fan critical | `inject_fault("fan", "Fan1A", health="Critical", speed_rpm=0)` |
| 2 | dell-r750 | Fan trending | `inject_gradual("fan", "Fan1A", field="speed_rpm", ...)` |
| 3 | dell-r750 | Disk SMART | `inject_fault("disk", "Disk.Bay.2", smart_alert=True, life_left_pct=18)` |
| 4 | dell-r750 | Disk critical | `inject_fault("disk", "Disk.Bay.2", health="Critical")` |
| 5 | dell-r750 | Memory ECC | `inject_fault("memory", "DIMM.Socket.A1", alarm_ecc_correctable=True, ecc_correctable_lifetime=150)` |
| 6 | dell-r750 | Memory critical | `inject_fault("memory", "DIMM.Socket.A1", health="Critical", alarm_ecc_uncorrectable=True)` |
| 7 | dell-r750 | PSU absent | `inject_fault("psu", "PS2", state="Absent", redundancy_health="Warning")` |
| 8 | dell-r750 | Thermal warning | `inject_fault("thermal", "Inlet", reading_c=44)` |
| 9 | dell-r750 | Thermal critical | `inject_fault("thermal", "CPU1", reading_c=100)` |
| 10 | dell-r750 | Cross-subsystem | Fan degradation + thermal rise (combined faults) |
| 11 | hpe-dl360-gen10 | Fan critical | `inject_fault("fan", "Fan 1", health="Critical", speed_rpm=0)` |
| 12 | hpe-dl360-gen10 | Fan trending | `inject_gradual("fan", "Fan 1", field="speed_rpm", ...)` |
| 13 | hpe-dl360-gen10 | Disk SMART | `inject_fault("disk", "Drive 2", smart_alert=True, life_left_pct=18)` |
| 14 | hpe-dl360-gen10 | Disk SMART (SmartStorage) | Inject via SmartStorage drive path |
| 15 | hpe-dl360-gen10 | Memory ECC | `inject_fault("memory", "proc1dimm1", alarm_ecc_correctable=True, ecc_correctable_lifetime=150)` |
| 16 | hpe-dl360-gen10 | Memory critical | `inject_fault("memory", "proc1dimm1", health="Critical")` |
| 17 | hpe-dl360-gen10 | PSU absent | `inject_fault("psu", "PS2", state="Absent", redundancy_health="Warning")` |
| 18 | hpe-dl360-gen10 | Thermal warning | `inject_fault("thermal", "Inlet Ambient", reading_c=44)` |
| 19 | dell-r760 | Fan critical | Same as path 1, different fixture data (DDR5, iDRAC10) |
| 20 | hpe-dl380-gen11 | Fan critical | Same as path 11, different fixture data (DDR5, iLO6, no SmartStorage) |
| 21 | dell-r750 | Peer down | `peer/stop` for simulated peer |
| 22 | dell-r750 | Full demo cascade | All faults in sequence (demo scenario) |

---

## 15. Demo Integration

### 15.1 Demo Controller Sequence

The `harken demo` command (Doc 9) uses the simulator's fault injection API to drive the 60-second scenario. The full injection sequence:

```python
DEMO_FAULTS = [
    # Phase 1: Healthy baseline (t=0 to t=5)
    # No faults -- simulator serves healthy fixtures

    # Phase 2: Fan degradation (t=5)
    (5, "inject-gradual", {
        "device": "dell-r750",
        "fault_type": "fan",
        "target": "Fan1A",
        "params": {
            "field": "speed_rpm",
            "start_value": 9800,
            "rate_per_second": -3.33,   # -200 RPM/minute
            "duration_seconds": 55,     # runs until demo end
            "update_interval_seconds": 2.0
        }
    }),

    # Phase 3: Disk SMART alert (t=15)
    (15, "inject-fault", {
        "device": "dell-r750",
        "fault_type": "disk",
        "target": "Disk.Bay.2",
        "params": {
            "health": "Warning",
            "life_left_pct": 18,
            "smart_alert": True
        }
    }),

    # Phase 4: Peer goes down (t=25)
    (25, "peer/stop", {
        "peer_id": PEER_1_UUID
    }),

    # Phase 5: PSU failure (t=35)
    (35, "inject-fault", {
        "device": "dell-r750",
        "fault_type": "psu",
        "target": "PS2",
        "params": {
            "state": "Absent",
            "health": "Critical",
            "redundancy_health": "Warning"
        }
    }),

    # Phase 6: Thermal cascade (t=50)
    (50, "inject-fault", {
        "device": "dell-r750",
        "fault_type": "thermal",
        "target": "Inlet",
        "params": {
            "reading_c": 28,
            "cpu1_reading_c": 62
        }
    }),
]
```

### 15.2 Time Compression

The `--speed` flag affects the demo controller's scheduling, not the simulator itself. The demo controller divides all `time_offset` values by the speed factor:

```python
actual_delay = time_offset / speed
```

The simulator does not have a concept of simulated time. Gradual faults run in real time. When `--speed 10`, the demo controller must adjust `rate_per_second` accordingly:

```python
adjusted_rate = base_rate * speed
adjusted_duration = base_duration / speed
```

---

## 16. Implementation Checklist

The following tasks must be completed to implement the mock simulator:

| # | Task | Module | Dependencies |
|---|------|--------|-------------|
| 1 | Create fixture directory structure | `harkeniq/mock/fixtures/` | None |
| 2 | Write healthy fixture JSON files for dell-r750 (all endpoints) | `fixtures/dell-r750/` | Doc 5 |
| 3 | Write healthy fixture JSON files for dell-r760 | `fixtures/dell-r760/` | Doc 5, task 2 (copy + modify) |
| 4 | Write healthy fixture JSON files for hpe-dl360-gen10 (including SmartStorage) | `fixtures/hpe-dl360-gen10/` | Doc 5 |
| 5 | Write healthy fixture JSON files for hpe-dl380-gen11 | `fixtures/hpe-dl380-gen11/` | Doc 5, task 4 (copy + modify, remove SmartStorage) |
| 6 | Implement FixtureLoader class | `harkeniq/mock/fixtures.py` | Tasks 2-5 |
| 7 | Implement DeviceState dataclass | `harkeniq/mock/state.py` | Task 6 |
| 8 | Implement SessionManager class | `harkeniq/mock/auth.py` | None |
| 9 | Implement Redfish route handlers | `harkeniq/mock/routes.py` | Tasks 7, 8 |
| 10 | Implement fault injection handlers | `harkeniq/mock/faults.py` | Task 7 |
| 11 | Implement gradual fault engine | `harkeniq/mock/gradual.py` | Task 10 |
| 12 | Implement peer simulator | `harkeniq/mock/peer.py` | None |
| 13 | Implement error simulation (latency, error rate, rate limit) | `harkeniq/mock/errors.py` | Task 9 |
| 14 | Implement MockSimulator class (orchestrator) | `harkeniq/mock/simulator.py` | Tasks 6-13 |
| 15 | Implement `harken mock` CLI commands | `harkeniq/cli/mock.py` | Task 14 |
| 16 | Implement self-signed cert generation | `harkeniq/mock/tls.py` | None |
| 17 | Write pytest fixtures (conftest) | `tests/conftest.py` | Task 14 |
| 18 | Write integration tests for all 22 test paths | `tests/integration/` | Task 17 |

---

## 17. Open Items

1. **Fixture accuracy:** The fixture JSON files in this spec are illustrative. Final fixtures must be validated against real iDRAC9, iDRAC10, iLO5, and iLO6 responses from the design partner's hardware (Doc 5, Section 12, Item 1).
2. **DIMM count templating:** Decide between 16 individual DIMM fixture files or a template-based generator. The template approach reduces maintenance but adds a build step.
3. **Drive IDs:** The Dell drive IDs (`Disk.Bay.0:Enclosure.Internal.0-1:RAID.Slot.1-1`) are long and complex. The target resolution logic should support both the full path and shorthand (`Disk.Bay.0`) for convenience.
4. **Redfish version strings:** The `@odata.type` version numbers in fixtures (e.g., `#Thermal.v1_7_0.Thermal`) should match the actual firmware versions deployed at the design partner site.
5. **HPE iLO5 SmartStorage + standard coexistence:** Confirm whether the standard `/Storage` path on iLO5 returns NVMe drives (expected) or is empty when SmartArray is the only controller. This affects which fixtures to include in the `hpe-dl360-gen10` profile.
