# HarkenIQ: Device + Protocol Telemetry Matrix

## Comprehensive Technical Reference
**Date:** 2026-08-01
**Purpose:** Map available telemetry, events, and actions per device+protocol combination for HarkenIQ's autonomous hardware ops platform.

---

## 1. Dell PowerEdge Servers (via iDRAC)

### 1.1 Redfish (Primary Protocol)

**Maturity:** Production-grade. iDRAC9/iDRAC10 are fully DMTF Redfish-conformant. Dell is the most aggressive Redfish implementer. iDRAC10 (16G servers) extends capabilities further. Requires iDRAC Datacenter license for telemetry streaming.

**Key Endpoints:**
| Endpoint | Data |
|----------|------|
| `/redfish/v1/Systems/System.Embedded.1` | System overview, health rollup, power state, boot options, BIOS attributes |
| `/redfish/v1/Chassis/System.Embedded.1/Thermal` | Temperature sensors (inlet, exhaust, CPU, DIMM, PCH), fan speeds/status |
| `/redfish/v1/Chassis/System.Embedded.1/Power` | PSU status, power consumption (watts), voltage rails, power cap settings |
| `/redfish/v1/Chassis/System.Embedded.1/Sensors` | Individual sensor readings (iDRAC9 4.x+) |
| `/redfish/v1/Systems/System.Embedded.1/Processors` | CPU health, model, cores, cache, thermal state |
| `/redfish/v1/Systems/System.Embedded.1/Memory` | DIMM health, capacity, speed, ECC error counts |
| `/redfish/v1/Systems/System.Embedded.1/Storage` | RAID controllers, virtual disks, physical drives, predictive failure |
| `/redfish/v1/Systems/System.Embedded.1/NetworkInterfaces` | NIC health, link status, MAC, firmware |
| `/redfish/v1/Managers/iDRAC.Embedded.1` | iDRAC firmware version, network config, licensing |
| `/redfish/v1/Managers/iDRAC.Embedded.1/LogServices` | Lifecycle Controller logs, SEL |
| `/redfish/v1/TelemetryService` | Metric report definitions, metric reports (280+ metrics) |
| `/redfish/v1/EventService` | Event subscriptions (SSE and HTTP Push) |
| `/redfish/v1/UpdateService` | Firmware inventory, update actions |

**Telemetry (280+ real-time metrics across 24 report categories):**
- Thermal: Inlet/exhaust/CPU/DIMM temperatures, fan RPM, fan PWM duty cycle
- Power: System power draw (W), PSU input/output power, voltage rails, current, power cap headroom
- CPU: Utilization %, frequency, C-state residency, thermal throttling events, IPC
- Memory: Bandwidth utilization, ECC correctable/uncorrectable error counts, page retirements
- Storage: Drive SMART data, predictive failure alerts, rebuild progress, IO latency
- Network: NIC throughput (Tx/Rx bytes), packet errors, link flaps, RDMA stats
- GPU: Temperature, power draw, utilization (when GPUs present)
- System: Boot time, POST codes, OS watchdog status

**Events (Alert + Lifecycle):**
- Hardware alerts: Fan failure, PSU failure/redundancy loss, temperature threshold crossed, memory ECC uncorrectable, drive predictive failure, RAID degraded, CPU internal error
- Lifecycle events: Firmware update started/completed, configuration change, job completion
- Subscription: SSE (Server-Sent Events) or HTTPS Push to event listener
- Severity levels: Critical, Warning, Informational

**Actions:**
| Action Endpoint | Capability |
|-----------------|------------|
| `ComputerSystem.Reset` | Power On, ForceOff, GracefulShutdown, ForceRestart, Nmi, PushPowerButton |
| `Oem/DellOemChassis.ExtendedReset` | PowerCycle (virtual AC cycle) |
| `Bios.ResetBios` | Reset BIOS to defaults |
| `Bios.ChangePassword` | Change BIOS password |
| BIOS Attributes PATCH | Change boot order, enable/disable features |
| `UpdateService.SimpleUpdate` | Push firmware update |
| `Manager.Reset` | Restart iDRAC |
| Virtual Media Insert/Eject | Mount ISO for remote boot |

**Critical Faults Detectable:**
- PSU failure (immediate) and redundancy loss (pre-failure)
- Fan failure leading to thermal shutdown
- Memory uncorrectable ECC (server crash imminent)
- Drive predictive failure (SMART trip)
- RAID controller battery failure
- CPU thermal throttling / THERMTRIP
- System board voltage regulator failure
- NIC link-down / CRC errors

---

### 1.2 IPMI (Legacy, still supported)

**Maturity:** Fully supported via iDRAC BMC. IPMI 2.0 over LAN. Being deprecated in favor of Redfish but universally available.

**Sensor Data (via SDR - Sensor Data Records):**
- Temperature: ~20-40 sensors (inlet, exhaust, each CPU, DIMMs, PCH, backplane, PSU)
- Voltage: 3.3V, 5V, 12V rails, CPU Vcore, DIMM voltage
- Fan: RPM for each fan module (typically 6-8 fans in 2U)
- Power: System wattage, PSU status (discrete), PSU redundancy
- Current: PSU input current
- Intrusion: Chassis open/closed

**SEL Events (System Event Log):**
- Temperature threshold crossed (upper critical, upper non-critical)
- Voltage out of range
- Fan failure / below threshold RPM
- Power supply failure, AC lost, predictive failure
- Memory ECC single-bit (correctable) and multi-bit (uncorrectable)
- CPU IERR (Internal Error), THERMTRIP
- Watchdog timer expiry (OS hang detection)
- Drive failure, RAID status change
- POST errors
- Chassis intrusion
- Each event includes: Timestamp, Sensor Type, Event Direction (assert/deassert), Event Data

**Chassis Controls:**
- Power On / Power Off / Power Cycle / Hard Reset
- NMI (Non-Maskable Interrupt - for kernel crash dump)
- Identify LED on/off (chassis locate)
- Boot device override (PXE, HDD, CD, BIOS Setup)
- Get/Set power restore policy (always-on, always-off, last-state)

**Critical Events:**
- Sensor Type 01h: Temperature - Upper Critical threshold
- Sensor Type 02h: Voltage - Lower Critical threshold
- Sensor Type 04h: Fan - Lower Critical (failure)
- Sensor Type 08h: Power Supply - Failure, Predictive Failure
- Sensor Type 0Ch: Memory - Uncorrectable ECC
- Sensor Type 07h: Processor - IERR, Thermal Trip
- Sensor Type 0Fh: System Firmware - POST Error
- Sensor Type 13h: Critical Interrupt - NMI, Bus Timeout

---

### 1.3 SNMP

**Maturity:** Fully supported via iDRAC. Supports SNMPv2c and SNMPv3.

**MIBs:**
- `iDRAC-SMIv2.mib` - Primary iDRAC MIB (Enterprise OID: 1.3.6.1.4.1.674.10892.2)
- `DELL-RAC-MIB` - RAC-specific objects
- `IDRAC-MIB-SMIv2` - iDRAC9/10 updated MIB

**Key OIDs (Enterprise 1.3.6.1.4.1.674.10892.5):**
| OID Suffix | Metric |
|------------|--------|
| .1.1.1 (systemStateTable) | Overall system health rollup |
| .2.1 (systemBIOSTable) | BIOS version, status |
| .2.2 (firmwareTable) | All firmware versions |
| .3.2.1.0.2650 | CPU utilization % |
| .4.200 (temperatureProbeTable) | All temperature readings |
| .4.600 (coolingDeviceTable) | Fan status and RPM |
| .4.700 (voltageProbeTable) | Voltage readings |
| .5.4 (powerSupplyTable) | PSU status, wattage |
| .5.1 (powerUnitTable) | Power unit redundancy status |
| .6 (memoryDeviceTable) | DIMM status, errors |
| .7 (networkDeviceTable) | NIC link status |

**Traps:**
- alertSystemUp / alertSystemDown
- alertTemperatureProbeNormal / Warning / Failure
- alertCoolingDeviceNormal / Warning / Failure
- alertVoltageProbeNormal / Warning / Failure
- alertPowerSupplyNormal / Warning / Failure
- alertMemoryDeviceNormal / Warning / Failure
- alertStorageDeviceNormal / Warning / Failure (physical disk)
- alertBatteryNormal / Warning / Failure
- alertRedundancyLost / alertRedundancyDegraded
- alertIntegrityFailure (chassis intrusion)

---

### 1.4 gRPC

**Applicability:** NOT natively supported on iDRAC. Dell's telemetry streaming uses Redfish SSE/HTTP push or direct export to collectors (rsyslog, Kafka). No gRPC dial-in/dial-out on the BMC itself.

**Workaround:** Use iDRAC Telemetry Reference Tools (open-source) to bridge iDRAC telemetry into gRPC-compatible collectors (Telegraf, etc).

---

### 1.5 DCGM

**Applicability:** NOT applicable to iDRAC itself. DCGM runs at the OS/host level to monitor NVIDIA GPUs installed in the PowerEdge server. See Section 5 below for DCGM details. iDRAC can report basic GPU health via Redfish when GPUs are present, but DCGM provides far deeper instrumentation.

---

## 2. HPE ProLiant Servers (via iLO)

### 2.1 Redfish (Primary Protocol)

**Maturity:** Production-grade. iLO 5 (Gen10/Gen10+) and iLO 6 (Gen11) are fully Redfish-conformant. HPE extends with OEM `Hpe` namespace for advanced features. Telemetry service available on Intel-based servers (iLO 5/6), excluding DL20/ML30/MicroServer.

**Key Endpoints:**
| Endpoint | Data |
|----------|------|
| `/redfish/v1/Systems/1` | System health, power state, BIOS, boot options |
| `/redfish/v1/Chassis/1/Thermal` | Temperature sensors, fan readings |
| `/redfish/v1/Chassis/1/Power` | PSU info, power consumption, power limit |
| `/redfish/v1/Systems/1/Processors` | CPU health, status, model |
| `/redfish/v1/Systems/1/Memory` | DIMM health, status, errors |
| `/redfish/v1/Systems/1/Storage` | SmartArray controllers, drives, volumes |
| `/redfish/v1/Systems/1/EthernetInterfaces` | NIC status, speed, link |
| `/redfish/v1/Managers/1` | iLO firmware, network config |
| `/redfish/v1/TelemetryService` | Metric reports and definitions |
| `/redfish/v1/TelemetryService/MetricReportDefinitions` | CPUUtil, MemoryBusUtil, IOBusUtil, PowerMetrics, etc. |
| `/redfish/v1/EventService` | Event subscriptions |
| `/redfish/v1/UpdateService` | Firmware management, component repository, install sets |
| `/redfish/v1/Systems/1/LogServices/IML` | Integrated Management Log |
| `/redfish/v1/Managers/1/LogServices/IEL` | iLO Event Log |

**Telemetry (Metric Report Definitions):**
- CPUUtil: CPU utilization %
- MemoryBusUtil: Memory bus utilization
- IOBusUtil: IO bus utilization
- CPUICUtil: CPU interconnect utilization
- JitterCount: System jitter events
- PowerMetrics: System power consumption
- AvgCPU0Freq / AvgCPU1Freq: Per-socket average frequency
- CPU0Power / CPU1Power: Per-socket power consumption
- Fan speeds, temperatures (via Thermal resource)

**Events:**
- IML (Integrated Management Log): Server health events with severity, repair actions
- iLO Event Log (IEL): iLO-specific events (login, config changes)
- AHS (Active Health System): Deep diagnostic log (1400+ parameters, always-on recording)
- Event subscription: HTTPS POST to listeners
- Event types: StatusChange, ResourceUpdated, ResourceAdded, ResourceRemoved, Alert

**Actions:**
| Action | Capability |
|--------|------------|
| `ComputerSystem.Reset` | On, ForceOff, GracefulShutdown, ForceRestart, Nmi, PushPowerButton |
| `UpdateService` actions | Firmware update via component repository, install sets, update queue |
| `HpeComponentInstallSet.Invoke` | Execute firmware install set |
| Virtual Media mount/eject | Remote ISO boot |
| BIOS configuration PATCH | Change BIOS settings (pending reboot) |
| iLO Reset | Restart iLO processor |
| License management | Add/remove iLO license keys |
| One-button secure erase | System sanitization |

**Critical Faults Detectable:**
- Fan failure / degraded cooling
- PSU failure / redundancy lost
- Temperature critical threshold
- Memory uncorrectable ECC / DIMM failure
- Drive predictive failure / SmartArray degraded
- CPU internal error
- PCIe bus errors
- iLO self-test failure
- System board failure indicators

---

### 2.2 IPMI

**Maturity:** Supported on iLO (IPMI 2.0 over LAN). iLO acts as the BMC. HPE recommends Redfish over IPMI for new deployments.

**Sensor Data:**
- Temperature: Ambient, CPU, memory, storage backplane, PSU, chipset (~15-30 sensors)
- Voltage: CPU Vcore, DIMM voltage, system board rails
- Fan: Each fan zone RPM (typically 6-10 fans)
- Power: System input power (W), PSU presence/status
- Discrete: Drive status, RAID status, PSU status

**SEL Events:**
- Temperature: Upper Non-Critical, Upper Critical, Upper Non-Recoverable
- Fan: Lower Critical, Lower Non-Recoverable (failure)
- PSU: Presence, Failure, Predictive Failure
- Memory: Correctable ECC, Uncorrectable ECC, Parity
- Processor: IERR, Thermal Trip, Configuration Error
- System Event: POST Error, OS Boot Failure, Watchdog
- Critical Interrupt: NMI, PCI PERR, PCI SERR, Bus Timeout

**Chassis Controls:**
- Same as Dell: Power On/Off/Cycle/Reset, NMI, Boot Device, Identify LED, Power Restore Policy

---

### 2.3 SNMP

**Maturity:** Fully supported. SNMPv1/v2c/v3.

**MIBs (HPE Insight Management MIBs):**
| MIB | Coverage |
|-----|----------|
| CPQHLTH-MIB (.1.3.6.1.4.1.232.6) | Overall health, fans, temperatures, power, memory |
| CPQIDA-MIB (.1.3.6.1.4.1.232.3) | Smart Array RAID controllers, logical/physical drives |
| CPQSINFO-MIB (.1.3.6.1.4.1.232.2) | System information, asset, model, serial |
| CPQNIC-MIB (.1.3.6.1.4.1.232.18) | NIC health, link status, errors |
| CPQSM2-MIB (.1.3.6.1.4.1.232.9) | iLO management processor |
| CPQRACK-MIB (.1.3.6.1.4.1.232.22) | Enclosure/blade chassis |
| CPQHOST-MIB (.1.3.6.1.4.1.232.11) | OS and host agent information |
| CPQSTDEQ-MIB (.1.3.6.1.4.1.232.1) | Standard equipment |

**Key OIDs:**
- `.1.3.6.1.4.1.232.6.2.6` (cpqHeFltTolFanTable): Fan status per fan
- `.1.3.6.1.4.1.232.6.2.6.7.1.9`: Fan condition (ok/degraded/failed)
- `.1.3.6.1.4.1.232.6.2.15` (cpqHeTemperatureTable): Temperature sensors
- `.1.3.6.1.4.1.232.6.2.9` (cpqHeFltTolPowerSupplyTable): PSU status
- `.1.3.6.1.4.1.232.6.2.14.13` (cpqHeResMem2ModuleTable): DIMM status per module
- `.1.3.6.1.4.1.232.3.2.3` (cpqDaPhyDrvTable): Physical drive status
- `.1.3.6.1.4.1.232.3.2.2` (cpqDaLogDrvTable): Logical volume status

**Traps:**
- cpqHe3FltTolFanDegraded / cpqHe3FltTolFanFailed
- cpqHe3TemperatureDegraded / cpqHe3TemperatureFailed
- cpqHe3FltTolPowerSupplyDegraded / cpqHe3FltTolPowerSupplyFailed
- cpqHe4CorrMemReplaceMemModule (correctable memory threshold)
- cpqDa6PhyDrvStatusChange (drive degraded/failed/predictive failure)
- cpqDa6LogDrvStatusChange (RAID degraded/failed)
- cpqSm2ServerPowerOff / cpqSm2ServerReset

---

### 2.4 gRPC

**Applicability:** NOT natively supported on iLO. HPE does not expose a gRPC interface on the BMC. Same situation as Dell iDRAC.

---

### 2.5 DCGM

**Applicability:** Same as Dell -- DCGM is an OS-level tool for NVIDIA GPUs in the server. Not part of iLO. See Section 5.

---

## 3. Cisco Switches (Nexus 9000/7000 + Catalyst 9000)

### 3.1 Redfish

**Applicability:** NOT supported on Cisco Nexus or Catalyst network switches. Redfish is a server BMC standard. Cisco switches do not have a Redfish-conformant BMC. (Note: Cisco UCS C-Series *servers* do support Redfish via CIMC, but that is not relevant here.)

---

### 3.2 IPMI

**Applicability:** NOT applicable. Cisco Nexus/Catalyst switches do not expose IPMI. IPMI is a server motherboard/BMC protocol. Switches use their own NX-OS/IOS-XE management plane.

---

### 3.3 SNMP (Well-supported)

**Maturity:** Fully mature. SNMPv1/v2c/v3. This is the traditional monitoring protocol for network switches.

**MIBs (Cisco Nexus 9000):**
| MIB | Coverage |
|-----|----------|
| IF-MIB (ifTable, ifXTable) | Interface counters (bytes, packets, errors, discards, speed, admin/oper status) |
| ENTITY-MIB | Physical inventory (modules, PSUs, fans, transceivers) |
| CISCO-ENTITY-FRU-CONTROL-MIB | FRU power status, fan tray status |
| CISCO-ENTITY-SENSOR-MIB | Temperature sensors, voltage, current, fan RPM |
| CISCO-PROCESS-MIB | CPU utilization, memory utilization |
| CISCO-MEMORY-POOL-MIB | Memory pool usage |
| CISCO-ENVMON-MIB | Environmental monitoring (temp, voltage, fan, PSU status) |
| BGP4-MIB / CISCO-BGP4-MIB | BGP session status, prefix counts |
| OSPF-MIB | OSPF neighbor status |
| CISCO-VPC-MIB | vPC peer status |
| CISCO-FCC-MIB / FC-FE-MIB | Fibre Channel (Nexus with FC modules) |
| LLDP-MIB | Neighbor discovery |
| BRIDGE-MIB / Q-BRIDGE-MIB | VLAN, MAC table |
| CISCO-HSRP-MIB | First-hop redundancy |
| CISCO-IF-EXTENSION-MIB | Extended interface stats |

**Key OIDs:**
- `1.3.6.1.2.1.2.2.1` (ifTable): Interface admin/oper status, speed, errors
- `1.3.6.1.2.1.31.1.1` (ifXTable): 64-bit counters (HC), interface name
- `1.3.6.1.4.1.9.9.109.1.1.1` (cpmCPUTotalTable): CPU utilization
- `1.3.6.1.4.1.9.9.305` (ciscoEntitySensorMIB): Temperature, PSU sensors
- `1.3.6.1.4.1.9.9.117` (ciscoEntityFruControl): FRU operational status

**Traps/Notifications:**
- linkDown / linkUp (interface flap)
- ciscoEnvMonTemperatureNotification (temp threshold)
- ciscoEnvMonFanNotification (fan failure)
- ciscoEnvMonRedundantSupplyNotification (PSU failure)
- cefcFRURemoved / cefcFRUInserted (line card, PSU hot-swap)
- cefcFanTrayStatusChange
- bgpEstablished / bgpBackwardTransition (BGP peer down)
- ospfNbrStateChange
- ciscoVpcPeerLinkDown
- stpNewRoot / stpTopologyChange (STP events)

---

### 3.4 gRPC (Primary modern telemetry protocol for Nexus)

**Maturity:** Production-grade on Nexus 9000 (NX-OS 7.0(3)I5+ and all 10.x releases). Also supported on Nexus 7000 (8.x+) and Catalyst 9000 (IOS-XE 16.10+). This is Cisco's recommended path for real-time telemetry.

**Transport & Encoding:**
- gRPC with GPB (Google Protocol Buffers) encoding -- highest performance
- gRPC with JSON encoding -- easier debugging
- gNMI (gRPC Network Management Interface) -- standard IETF approach
- Dial-out (switch pushes to collector) and Dial-in (collector pulls from switch via gNMI)

**Telemetry Data Sources:**

| DME/YANG Path | Data Category |
|---------------|---------------|
| `sys/intf` | Interface counters (bytes, packets, errors, CRC, giants, runts), oper/admin status |
| `sys/bgp` | BGP sessions, prefix counts, state changes, AS path |
| `sys/ospf` | OSPF adjacencies, state, LSA counts |
| `sys/vpc` | vPC status, peer-link health, consistency |
| `sys/fm` | Feature manager (which features enabled) |
| `sys/eps` (VXLAN) | VXLAN endpoints, tunnel status |
| `sys/mac` | MAC address table |
| `sys/arp` | ARP table |
| `sys/lldp` | LLDP neighbor info |
| `sys/ch` (Environment) | Chassis: fan status/RPM, temperature sensors, PSU status/power draw, supervisor/line card status |
| `sys/procsys` | CPU utilization, process info, memory |
| `sys/acl` | ACL statistics (hit counts per rule) |
| `sys/platform` | TCAM utilization, forwarding resources |

**Cisco Catalyst 9000 (IOS-XE) gRPC Telemetry:**
- YANG models: openconfig-interfaces, Cisco-IOS-XE-environment-oper, Cisco-IOS-XE-process-cpu-oper
- gNMI subscribe for interface stats, CPU, memory, environment
- Supports periodic and on-change subscriptions
- Transport: gRPC dial-out, gNMI dial-in

**Event-Based Streaming:**
- On-change telemetry: Only sends updates when a value changes (zero polling overhead)
- Frequency-based: Configurable intervals (minimum ~5 seconds practical, typically 10-30s)
- High availability: Telemetry survives supervisor switchover

**Critical Faults Detectable via gRPC:**
- Interface down (instant notification via on-change)
- BGP peer flap / session loss
- Fan/PSU failure (environment path)
- Temperature threshold exceeded
- vPC peer-link failure
- High CPU/memory utilization
- CRC errors / interface errors (early indicator of optic or cable failure)
- TCAM exhaustion

---

### 3.5 DCGM

**Applicability:** NOT applicable. Network switches do not contain NVIDIA GPUs. DCGM has zero relevance to Cisco Nexus/Catalyst.

---

## 4. Dell PowerScale (Storage)

### 4.1 Redfish

**Applicability:** NOT supported. PowerScale runs OneFS (a clustered NAS OS), not a server BMC. PowerScale nodes do not expose a Redfish interface. Management is via the OneFS Platform REST API (proprietary).

**Alternative - OneFS Platform REST API:**

**Base URL:** `https://<cluster-ip>:8080/platform/<version>/`

**Key Endpoints:**
| Endpoint | Data |
|----------|------|
| `/platform/12/cluster/health` | Overall cluster health status |
| `/platform/12/cluster/nodes` | Node list, status, hardware info |
| `/platform/12/cluster/nodes/<id>/hardware` | Per-node hardware health (drives, NICs, PSUs, fans) |
| `/platform/12/cluster/nodes/<id>/status` | Node operational status, uptime |
| `/platform/12/statistics/current` | Real-time performance metrics |
| `/platform/12/statistics/history` | Historical performance data |
| `/platform/12/event/eventgroup-occurrences` | Active events/alerts by severity |
| `/platform/12/event/eventlists` | Event definitions |
| `/platform/12/storagepool/storagepools` | Storage tier status, capacity |
| `/platform/12/quota/quotas` | Quota usage and limits |
| `/platform/12/protocols/smb/shares` | SMB share config/status |
| `/platform/12/protocols/nfs/exports` | NFS export config/status |
| `/platform/12/sync/policies` | SyncIQ replication status |
| `/platform/12/healthcheck/evaluations` | On-demand health check |
| `/platform/12/job/jobs` | Job Engine status (SmartPools, FlexProtect, etc.) |

**Telemetry (Statistics API):**
- Protocol performance: NFS/SMB/S3/HDFS ops/sec, latency percentiles, throughput (MB/s)
- Disk performance: Read/write IOPS, latency per drive
- Network: Throughput per interface, packet errors
- CPU: Utilization per node
- Cluster capacity: Total/used/free, dedup/compression ratios
- Node-level: Per-node contribution to cluster workload

**Events:**
- Hardware: Drive failure, node offline, PSU failure, fan failure, temperature alarm
- Software: Filesystem full, quota exceeded, job failure, replication failure
- Cluster: Split-brain, node join/leave, group change
- Severity levels: Emergency, Critical, Warning, Information

**Actions:**
- Node shutdown/reboot: `POST /platform/12/cluster/nodes/<id>/reboot`
- Job control: Start/stop/pause SmartPools, FlexProtect, IntegrityScan jobs
- SmartFail drive: Initiate controlled drive evacuation
- Add/remove node from cluster
- SyncIQ policy start/stop

**Critical Faults:**
- Drive failure (data at risk if below protection level)
- Node offline (degraded performance and protection)
- FlexProtect running (cluster rebuilding data -- vulnerable state)
- Journal overflow
- Cluster split-brain
- Network partition between nodes

---

### 4.2 IPMI

**Applicability:** Limited. Individual PowerScale nodes contain a BMC accessible via IPMI for basic hardware management (power control, sensor readings). However, this is not the recommended management path -- OneFS API is preferred. The IPMI interface provides basic node-level power/thermal/voltage sensors similar to any server BMC.

---

### 4.3 SNMP

**Maturity:** Supported. SNMPv2c (default) and SNMPv3 in read-only mode.

**MIBs:**
| MIB | Location | Coverage |
|-----|----------|----------|
| ISILON-MIB | `/usr/share/snmp/mibs/` on node | Cluster and node health, performance stats |
| ISILON-TRAP-MIB | Same | Event/alert traps |

**Enterprise OID:** 1.3.6.1.4.1.12124 (Isilon/Dell EMC)

**Key Capabilities:**
- Hardware monitoring: Fans, temperature sensors, PSUs, disk health per node
- Cluster health: Overall cluster status, node count, degraded status
- Performance: Protocol operations/sec, throughput, latency
- Capacity: Total/used/available storage
- Proxy feature: Query any node through any other node (append `_node_<N>` to community string)

**Traps (ISILON-TRAP-MIB):**
- Hardware events: Disk failure, PSU failure, fan failure, temperature alarm
- Cluster events: Node down, node joined, group change
- Software events: Filesystem nearly full, quota exceeded, job failure
- Replication events: SyncIQ policy failure

---

### 4.4 gRPC

**Applicability:** NOT supported. PowerScale/OneFS does not expose a gRPC telemetry interface.

---

### 4.5 DCGM

**Applicability:** NOT applicable. Storage arrays do not contain NVIDIA GPUs.

---

## 5. NVIDIA DCGM (Data Center GPU Manager)

**Applies to:** Dell PowerEdge and HPE ProLiant servers with NVIDIA GPUs installed.
**Does NOT apply to:** Cisco switches, Dell PowerScale.

### 5.1 Overview

DCGM runs as a host-level daemon (dcgm-nv-hostengine) on the server OS. It communicates with NVIDIA GPUs via the NVIDIA kernel driver. It is independent of iDRAC/iLO -- those BMCs cannot access DCGM data directly (though iDRAC can report basic GPU health via Redfish separately).

### 5.2 Telemetry (Field IDs)

**Utilization Metrics:**
| Field ID | Metric |
|----------|--------|
| DCGM_FI_DEV_GPU_UTIL | GPU utilization % |
| DCGM_FI_DEV_MEM_COPY_UTIL | Memory controller utilization % |
| DCGM_FI_DEV_ENC_UTIL | Encoder utilization % |
| DCGM_FI_DEV_DEC_UTIL | Decoder utilization % |
| DCGM_FI_PROF_SM_ACTIVE | SM active ratio (cycles with >= 1 warp) |
| DCGM_FI_PROF_SM_OCCUPANCY | SM occupancy (warps resident vs max) |
| DCGM_FI_PROF_PIPE_TENSOR_ACTIVE | Tensor core active ratio |
| DCGM_FI_PROF_PIPE_FP64_ACTIVE | FP64 pipe utilization |
| DCGM_FI_PROF_PIPE_FP32_ACTIVE | FP32 pipe utilization |
| DCGM_FI_PROF_PIPE_FP16_ACTIVE | FP16 pipe utilization |
| DCGM_FI_PROF_DRAM_ACTIVE | DRAM active ratio |

**Thermal & Power:**
| Field ID | Metric |
|----------|--------|
| DCGM_FI_DEV_GPU_TEMP | GPU die temperature (C) |
| DCGM_FI_DEV_MEMORY_TEMP | HBM/GDDR memory temperature (C) |
| DCGM_FI_DEV_POWER_USAGE | Current power draw (W) |
| DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION | Cumulative energy (mJ) since boot |
| DCGM_FI_DEV_SLOWDOWN_TEMP | Thermal slowdown threshold |
| DCGM_FI_DEV_SHUTDOWN_TEMP | Thermal shutdown threshold |
| DCGM_FI_DEV_POWER_MGMT_LIMIT | Current power limit (W) |
| DCGM_FI_DEV_ENFORCED_POWER_LIMIT | Enforced power limit |

**Memory:**
| Field ID | Metric |
|----------|--------|
| DCGM_FI_DEV_FB_FREE | Free framebuffer memory (MB) |
| DCGM_FI_DEV_FB_USED | Used framebuffer memory (MB) |
| DCGM_FI_DEV_FB_TOTAL | Total framebuffer memory (MB) |
| DCGM_FI_DEV_FB_RESERVED | Reserved framebuffer memory (MB) |

**Reliability / Errors:**
| Field ID | Metric |
|----------|--------|
| DCGM_FI_DEV_ECC_SBE_VOL | Single-bit ECC errors (volatile, since driver load) |
| DCGM_FI_DEV_ECC_DBE_VOL | Double-bit ECC errors (volatile) |
| DCGM_FI_DEV_ECC_SBE_AGG | Single-bit ECC errors (aggregate, lifetime) |
| DCGM_FI_DEV_ECC_DBE_AGG | Double-bit ECC errors (aggregate, lifetime) |
| DCGM_FI_DEV_RETIRED_SBE | Pages retired due to single-bit errors |
| DCGM_FI_DEV_RETIRED_DBE | Pages retired due to double-bit errors |
| DCGM_FI_DEV_RETIRED_PENDING | Pages pending retirement |
| DCGM_FI_DEV_XID_ERRORS | Last XID error code |
| DCGM_FI_DEV_PCIE_REPLAY_COUNTER | PCIe replay (retransmit) count |
| DCGM_FI_DEV_NVLINK_BANDWIDTH_L0-L5 | NVLink bandwidth per link |
| DCGM_FI_DEV_NVLINK_CRC_FLIT_ERROR_COUNT | NVLink CRC errors |
| DCGM_FI_DEV_NVLINK_CRC_DATA_ERROR_COUNT | NVLink data CRC errors |

**Clock & Throttling:**
| Field ID | Metric |
|----------|--------|
| DCGM_FI_DEV_SM_CLOCK | SM clock frequency (MHz) |
| DCGM_FI_DEV_MEM_CLOCK | Memory clock frequency (MHz) |
| DCGM_FI_DEV_CLOCK_THROTTLE_REASONS | Bitmask of throttle reasons (power, thermal, etc.) |

**Fabric (Blackwell+):**
| Field ID | Metric |
|----------|--------|
| DCGM_FI_DEV_FABRIC_HEALTH_SUMMARY | GPU fabric health summary |
| NVSwitch telemetry fields | NVSwitch counters and health (via NVSDM backend) |

### 5.3 Health Checks & Diagnostics

**Three diagnostic levels:**
| Level | Duration | Tests |
|-------|----------|-------|
| r1 (Short) | < 2.5 seconds | NVML library, CUDA library, driver conflicts, permissions, basic GPU access |
| r2 (Medium) | 2.5-10.5 minutes | All r1 + PCIe bandwidth test, NVLink test, framebuffer test (targeted memory patterns), compute test (basic GEMM) |
| r3 (Long) | 10-35 minutes | All r2 + Power stress, thermal stress, sustained compute stress, memory bandwidth stress, extended ECC check |

**Health Watch System (continuous):**
- GPU health watches (monitors for XID errors, thermal events, power events)
- Memory health watches (ECC error accumulation, page retirements approaching limit)
- PCIe health watches (replay counter rate)
- NVLink health watches (CRC error rate, link down)
- Thermal health watches (approaching slowdown/shutdown thresholds)

### 5.4 Critical GPU Faults Detectable

| XID Code | Fault | Impact |
|----------|-------|--------|
| XID 13 | Graphics Engine Exception | GPU process crash |
| XID 31 | GPU Memory Page Fault | Potential data corruption |
| XID 43 | GPU stopped responding | GPU hang, requires reset |
| XID 45 | Preemptive cleanup - GPU removed | GPU fell off bus |
| XID 48 | Double-bit ECC error | Uncorrectable, data corrupt |
| XID 62 | Thermal violation | GPU overheating, throttle/shutdown |
| XID 63 | ECC page retirement limit | Row remapping exhausted |
| XID 64 | Fallen off bus | GPU hardware failure |
| XID 74 | NVLink error | Interconnect degraded |
| XID 79 | GPU access to memory not possible | Fatal memory failure |
| XID 94 | Contained ECC error | Correctable but monitored |
| XID 95 | Uncontained ECC error | Fatal, GPU unusable |

### 5.5 Actions via DCGM

- Set power limit per GPU
- Set clock frequency limits
- Enable/disable ECC mode (requires GPU reset)
- Run on-demand diagnostics (r1/r2/r3)
- Reset GPU (via nvidia-smi or fabric manager)
- Configure compute mode (exclusive, shared)
- Group management (monitor/manage GPUs in groups)

### 5.6 Integration

- **Prometheus:** dcgm-exporter exposes all metrics in Prometheus format
- **Kubernetes:** DCGM integrates with NVIDIA GPU Operator for K8s
- **API:** C library (libdcgm), Python bindings, Go bindings
- **Collection interval:** Configurable, typically 1-10 seconds

---

## 6. Compatibility Matrix (Quick Reference)

| Device | Redfish | IPMI | SNMP | gRPC | DCGM |
|--------|---------|------|------|------|------|
| Dell PowerEdge (iDRAC) | **Full** | **Full** | **Full** | N/A (use Redfish SSE) | Via host OS (GPU only) |
| HPE ProLiant (iLO) | **Full** | **Full** | **Full** | N/A | Via host OS (GPU only) |
| Cisco Nexus | N/A | N/A | **Full** | **Full** (primary) | N/A |
| Cisco Catalyst | N/A | N/A | **Full** | **Full** (IOS-XE 16.10+) | N/A |
| Dell PowerScale | N/A (use OneFS API) | Limited (node BMC) | **Full** | N/A | N/A |

---

## 7. Protocol Selection Guidance for HarkenIQ

### For Servers (Dell/HPE):
1. **Primary:** Redfish -- richest data, standards-based, supports events (SSE), actions, and telemetry streaming
2. **Fallback:** IPMI -- for legacy systems, boot-level control when Redfish unavailable
3. **Integration:** SNMP -- if existing NMS integration needed; traps for passive alerting
4. **GPU Monitoring:** DCGM at OS level -- essential for GPU health that Redfish cannot see

### For Switches (Cisco):
1. **Primary:** gRPC streaming telemetry -- real-time, low-latency, event-driven, scalable
2. **Fallback/Complement:** SNMP -- for devices not configured for gRPC, or for trap-based alerting
3. **Configuration:** NETCONF/RESTCONF (not covered here but worth noting)

### For Storage (PowerScale):
1. **Primary:** OneFS Platform REST API -- comprehensive, purpose-built
2. **Complement:** SNMP -- for trap-based alerting integration with existing NMS
3. **Limited:** IPMI only for emergency node-level power control

---

## 8. Key Architectural Implications for HarkenIQ

1. **No single protocol covers everything.** HarkenIQ must be multi-protocol from day one.
2. **Redfish is the unifying standard for servers** but does not apply to network or storage.
3. **gRPC is the future for network telemetry** with sub-second granularity and on-change notifications.
4. **DCGM is essential for GPU health** -- BMC-level GPU data is superficial compared to DCGM.
5. **Event-driven vs polling:** Redfish SSE, gRPC on-change, and SNMP traps all support push. Minimize polling.
6. **Licensing gates:** iDRAC Datacenter license required for telemetry streaming; iLO Advanced for full feature set.
7. **PowerScale is the outlier** -- proprietary REST API, no Redfish, limited IPMI. Requires dedicated adapter.

---

## Sources

- [Dell iDRAC Telemetry - Redfish API](https://developer.dell.com/apis/2978/versions/6.xx/docs/Tasks/3Telemetry.md)
- [Dell iDRAC10 Telemetry Reference Guide](https://www.dell.com/support/manuals/en-us/idrac10-lifecycle-controller-v1-xx-series/idrac_telemetry_reference_guide_pub/Overview-of-iDRAC-Telemetry)
- [Dell iDRAC10 User's Guide - Telemetry Streaming](https://www.dell.com/support/manuals/en-us/poweredge-r470/idrac10_1.xx_ug/telemetry-streaming?guid=guid-5afd8b6d-3465-4ddc-89ca-5fdb051ab512)
- [Dell OpenManage SNMP Reference Guide for iDRAC](https://www.dell.com/support/manuals/en-us/dell-openmanage-server-administrator-v8.3/snmp_idrac8/rac-traps?guid=guid-9c7f3e04-065b-4270-aff7-3694ca283b5a)
- [Dell iDRAC Redfish Eventing](https://www.dell.com/support/manuals/en-us/idrac9-lifecycle-controller-v3.0-series/idrac_3.00.00.00_redfishapiguide/eventing?guid=guid-ab574b6d-b473-4e10-9916-5d4f7e395e0d)
- [Dell iDRAC-Redfish-Scripting (GitHub)](https://github.com/dell/iDRAC-Redfish-Scripting)
- [Dell iDRAC-Telemetry-Reference-Tools (GitHub)](https://github.com/dell/iDRAC-Telemetry-Reference-Tools)
- [HPE iLO 6 Redfish API Reference](https://hewlettpackard.github.io/ilo-rest-api-docs/ilo6/)
- [HPE iLO 5 Redfish API Reference](https://hewlettpackard.github.io/ilo-rest-api-docs/ilo5/)
- [HPE iLO Telemetry Service](https://servermanagementportal.ext.hpe.com/docs/redfishservices/ilos/supplementdocuments/ilotelemetryservice)
- [HPE iLO Event Service](https://servermanagementportal.ext.hpe.com/docs/redfishservices/ilos/supplementdocuments/iloeventservices)
- [HPE Data Center Monitoring Using Redfish Telemetry (White Paper)](https://www.hpe.com/psnow/downloadDoc/Data%20center%20monitoring%20using%20Redfish%20telemetry%20and%20cloud-native%20tooling-a00134351enw.pdf)
- [HPE iLO RESTful API Developer Portal](https://developer.hpe.com/platform/ilo-restful-api/home/)
- [Cisco Nexus 9000 NX-OS Telemetry Guide (10.5)](https://www.cisco.com/c/en/us/td/docs/dcn/nx-os/nexus9000/105x/programmability/cisco-nexus-9000-series-nx-os-programmability-guide-105x/chapter-3.html)
- [Cisco Nexus 9000 Model-Driven Telemetry (10.2)](https://www.cisco.com/c/en/us/td/docs/dcn/nx-os/nexus9000/102x/programmability/cisco-nexus-9000-series-nx-os-programmability-guide-release-102x/m-n9k-model-driven-telemetry-101x.html)
- [Cisco Nexus 9000 Streaming Telemetry Sources (10.5)](https://www.cisco.com/c/en/us/td/docs/dcn/nx-os/nexus9000/105x/programmability/cisco-nexus-9000-series-nx-os-programmability-guide-105x/m-n9k-streaming-telemetry-sources-101x.html)
- [Cisco NX-OS MIB Quick Reference](https://www.cisco.com/c/en/us/td/docs/switches/datacenter/sw/mib/quickreference/cisco-nexus-7000-series-and-9000-series-nx-os-mib-quick-reference.html)
- [Cisco IOS-XE Model-Driven Telemetry White Paper](https://www.cisco.com/c/en/us/products/collateral/switches/catalyst-9300-series-switches/model-driven-telemetry-wp.html)
- [Dell PowerScale OneFS API Reference](https://www.dell.com/support/manuals/en-us/isilon-onefs/ifs_pub_onefs_api_reference/onefs-api-self-documentation?guid=guid-6a26bde5-592a-4064-9a12-4dde61526207)
- [Dell PowerScale Cluster Health and Events](https://www.dell.com/support/kbdoc/en-us/000223412/powerscale-how-to-check-the-cluster-health-and-unresolved-events)
- [Dell PowerScale HealthCheck API](https://developer.dell.com/apis/4088/versions/9.2.0.0/docs/Task/Perform%20health%20check%20on%20cluster.md)
- [Dell PowerScale SNMP Monitoring Guide](https://www.dell.com/support/manuals/en-us/isilon-onefs/ifs_pub_administration_guide_gui/snmp-monitoring?guid=guid-b812310b-b7b8-496f-834b-57e62290be17)
- [NVIDIA DCGM Documentation - Feature Overview](https://docs.nvidia.com/datacenter/dcgm/latest/user-guide/feature-overview.html)
- [NVIDIA DCGM Diagnostics](https://docs.nvidia.com/datacenter/dcgm/latest/user-guide/dcgm-diagnostics.html)
- [NVIDIA DCGM Field IDs (dcgm_fields.h)](https://github.com/NVIDIA/DCGM/blob/master/dcgmlib/dcgm_fields.h)
- [NVIDIA dcgm-exporter Metrics CSV](https://github.com/NVIDIA/dcgm-exporter/blob/main/etc/dcp-metrics-included.csv)
- [NVIDIA DCGM Exporter Documentation](https://docs.nvidia.com/datacenter/cloud-native/gpu-telemetry/latest/dcgm-exporter.html)
- [Redfish Telemetry Service Specification](https://redfish.redoc.ly/docs/concepts/redfishtelemetry/)
- [IPMI Specification v2.0](https://www.intel.com/content/dam/www/public/us/en/documents/product-briefs/ipmi-second-gen-interface-spec-v2-rev1-1.pdf)
