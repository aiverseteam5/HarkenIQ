# HarkenIQ Agent Capabilities Roadmap

**Document 4 of 4**
**Date:** 2026-08-11
**Status:** Draft for review
**Scope:** Complete agent-level ITOM capabilities across all releases. What the agent must do to be the sole intelligent operator on each device.

---

## 1. Positioning

The HarkenIQ agent is not a monitoring add-on. It is the **sole intelligent operator** on each device — an OS-resident process that makes dumb hardware self-aware. It replaces the human staring at iDRAC, iLO, OpenManage, OneView, SolarWinds, Nagios, and every other console. The vendor BMC (iDRAC, iLO) becomes the dumb sensor layer that HarkenIQ rides on top of.

**The intelligence architecture:**

| Layer | Role | Intelligence |
|-------|------|-------------|
| **Agent (on device OS)** | Observe + Act | Executes skills, collects telemetry, performs remediation |
| **Site Manager (per site)** | Learn + Reason | LLM-powered analysis, skill generation, approval workflow |
| **Cluster Manager (global)** | Federate + Optimize | Cross-site learning, global policy, fleet analytics |

The agent loop: **Observe → Learn → Reason → Act**
- **Observe:** Collect telemetry, logs, events from hardware (Redfish, IPMI, OS sensors)
- **Learn:** Baseline what "normal" looks like for this specific device
- **Reason:** Evaluate skills against observations, produce verdicts
- **Act:** Execute approved remediation, report results

Skills originate from the Site Manager (LLM-generated and human-authored), flow down to agents, and improve over time as the LLM learns from incident patterns across the fleet.

---

## 2. Crash Resilience Model

The agent runs on the OS of the managed server. If the server crashes, the agent dies with it. This is handled structurally:

| Observer | Detection Method | Response |
|----------|-----------------|----------|
| **Neighbour agents** | Peer heartbeat timeout | Flag device as unresponsive, retain pre-failure evidence (last 60s of observations from the dead neighbor) |
| **Site Manager** | Heartbeat + poll timeout | Trigger external remediation (power cycle via BMC Redfish from a surviving agent or directly) |
| **Mesh quorum** | Corroboration from multiple peers | Distinguish "device down" from "agent crashed but device is fine" from "network partition" |

**R-AGENT-1.** Agent crash must never be reported as device failure. Peers and Site Manager must distinguish the two.
**R-AGENT-2.** Agent must checkpoint state to survive restart without re-learning baselines from scratch.
**R-AGENT-3.** Agent must start collecting and diagnosing within 30 seconds of OS boot, without waiting for Site Manager contact.

---

## 3. The Eight Agent Capabilities

### Capability 1: Diagnosis (Telemetry + Log Collection + Skill Evaluation)

**Release: R1 (core)**

The foundational capability. The agent observes everything the device can report and evaluates it against skills.

#### 1a. Hardware Telemetry Collection

| Source | Data | Protocol |
|--------|------|----------|
| Thermal sensors | Inlet/exhaust/CPU/DIMM temperatures, fan RPM, fan PWM duty | Redfish Thermal |
| Power sensors | PSU status, power draw (W), voltage rails, current, power cap | Redfish Power |
| CPU | Utilization %, frequency, C-state residency, thermal throttle events | Redfish Processors |
| Memory | DIMM health, ECC correctable/uncorrectable counts, page retirements | Redfish Memory |
| Storage | Drive SMART, predictive failure, RAID health, rebuild progress | Redfish Storage |
| Network | NIC link status, throughput, packet errors, link flaps | Redfish NetworkInterfaces |
| GPU (when present) | Temperature, power, utilization, ECC errors | Redfish (vendor extensions) |

#### 1b. Log and Event Collection

| Source | Data | Value |
|--------|------|-------|
| System Event Log (SEL) | Hardware alerts: fan failure, PSU loss, thermal threshold, memory ECC uncorrectable, drive predictive failure, RAID degraded | Historical fault record |
| Lifecycle Controller logs | Firmware updates, config changes, job completions | Change tracking |
| OS syslog / journald | Kernel hardware errors (MCE, PCIe AER, disk I/O errors), driver messages | OS-level hardware symptoms |
| OS dmesg | Boot-time hardware detection, driver load failures, hardware error reports | Immediate hardware events |
| IPMI SEL (fallback) | Same as Redfish SEL for devices without full Redfish | Legacy device coverage |

#### 1c. Skill Evaluation

- Skills are YAML definitions with conditions, thresholds, and recommended actions
- Agent normalizes vendor-specific Redfish responses into a common data model before evaluation
- Verdicts: `HEALTHY`, `WARNING`, `CRITICAL`, `TRENDING`, `UNKNOWN`
- Every verdict carries evidence: the readings that triggered it, time range, confidence level

**R1 scope:** 5 fault types (fan, disk, memory, PSU, thermal) × 2 vendors (Dell, HPE) = 10 test paths minimum.

---

### Capability 2: Predictive Trending

**Release: R1 (core)**

Not all faults have a moment. A disk with rising ECC counts, a fan slowing over weeks, a PSU losing efficiency — these are progressive and invisible to threshold-based alerting until they cross into failure.

| Method | Application | Output |
|--------|-------------|--------|
| Linear regression on time-series | Temperature drift, fan RPM decline, ECC count growth, power efficiency loss | `TRENDING` verdict with projected time-to-threshold |
| Baseline deviation | Per-device learned normal → anomaly scoring | "This device's inlet temp is 4°C above its own historical baseline" |
| Rate-of-change detection | Sudden acceleration in error counts | "Memory ECC errors increased 10x in last 24 hours" |

**R-AGENT-4.** Trending must use per-device baselines, not fleet averages. A device in a hot aisle has a different "normal" than one in a cold aisle.
**R-AGENT-5.** Trending verdicts must include projected time-to-threshold and confidence interval.

**R2+ enhancement:** Site Manager LLM correlates trending patterns across fleet to identify systemic issues (e.g., a batch of drives from the same manufacturing lot all trending toward failure).

---

### Capability 3: Peer Heartbeat and Mesh Awareness

**Release: R1 (discovery + liveness), R2 (quorum + corroboration), R3 (full mesh)**

The agent is not alone. It knows its neighbors, monitors them, and uses their observations to produce better verdicts.

| Phase | Capability | Release |
|-------|-----------|---------|
| Discovery | Agent announces itself, discovers peers on same subnet/rack | R1 |
| Liveness | Periodic heartbeat, detect when a neighbor goes silent | R1 |
| Pre-failure evidence retention | Hold last 60s of a neighbor's reported state | R2 |
| Quorum disambiguation | Distinguish device-down vs agent-crash vs network-partition vs self-isolation (see §3.4 of architecture doc) | R2 |
| Corroborated diagnosis | Query neighbors before concluding on ambiguous faults | R2 |
| Incident ownership | First-claim with lease, deterministic tiebreak | R2 |
| Full mesh intelligence | Threshold-triggered claims, suspicion state exchange, cross-peer trending | R3 |

**R-AGENT-6.** An agent that cannot reach any peer must report on itself, not assume all peers are down.
**R-AGENT-7.** Heartbeat protocol must not generate enough traffic to be visible on production networks.

---

### Capability 4: Remediation / Autonomous Actions

**Release: R2 (basic actions), R3 (multi-step playbooks)**

Diagnosis without action is a better alert, not a replacement for human operators. The agent must be able to act — within strictly controlled boundaries.

#### 4a. Action Allow-List (R2)

| Action | Target | Risk | Pre-conditions |
|--------|--------|------|----------------|
| Blink chassis LED | Physical identification for tech dispatch | None | Always allowed |
| Clear SEL | Free log space after events are forwarded | Low | Only after events are captured and forwarded |
| Reset BMC (iDRAC/iLO) | Recover hung management controller | Low | After BMC unresponsive to 3 consecutive polls |
| Power cycle server | Recover hung OS/kernel panic | Medium | Requires Site Manager approval + neighbor corroboration that device is truly unresponsive |
| Adjust power cap | Respond to thermal or power budget events | Medium | Within policy-defined range only |
| Toggle port (future, R3) | Isolate a failing NIC/link | Medium | Only with neighbor corroboration of link fault |
| Clear DIMM page retirement list | Recover from correctable ECC exhaustion | Low | Only after upstream notification |

**R-AGENT-8.** Every action requires signed authorization from Site Manager, except LED blink and SEL clear which are locally authorized.
**R-AGENT-9.** Every action is idempotent, has an expiry, and has a defined rollback where physically possible.
**R-AGENT-10.** Refused, rate-limited, and expired actions are reported with the same weight as completed ones.
**R-AGENT-11.** No action may be executed against the device the agent is running on that would terminate the agent. Self-destructive actions (e.g., power cycle self) are delegated to a peer or the Site Manager.

#### 4b. Multi-Step Playbooks (R3)

Complex remediations require coordinated sequences:

| Playbook | Steps | Approval |
|----------|-------|----------|
| Disk replacement prep | Predict failure → mark drive for replacement → begin RAID rebuild on hot spare → verify rebuild → notify dispatch | Site Manager approval at step 1 |
| Thermal mitigation | Detect rising temps → reduce power cap → verify temp stabilization → if insufficient, graceful workload drain → notify facilities | Auto up to power cap; SM approval for drain |
| Memory remediation | Detect ECC errors rising → identify affected DIMM → page retirement if possible → if exhausted, recommend replacement → workload migration | SM approval before migration |
| NIC failover | Detect link degradation → verify bond/team member → disable degraded port → verify traffic on remaining members → dispatch for replacement | SM approval for port disable |

**R-AGENT-12.** Playbooks are checkpoint-resumable. If the agent or device crashes mid-playbook, the resumed agent picks up from the last completed step, not the beginning.
**R-AGENT-13.** Playbooks have a maximum execution window. A playbook that hasn't completed within its window escalates to Site Manager rather than continuing autonomously.

---

### Capability 5: Configuration Compliance

**Release: R2 (detection), R3 (remediation)**

The agent knows what the device's configuration *should* be and detects when it drifts. This catches the silent failures: someone changes BIOS settings during debug and forgets, a firmware update resets BMC config, a security policy isn't applied uniformly.

#### 5a. Configuration Domains

| Domain | What is Compared | Source of Truth |
|--------|------------------|-----------------|
| BIOS settings | Boot order, virtualization, hyperthreading, power profile, security settings | Golden baseline from Site Manager (per device model/role) |
| BMC settings | Network config, LDAP/AD auth, TLS version, NTP source, session timeout, SNMP community | Security policy from Site Manager |
| Storage config | RAID level, stripe size, write policy, patrol read schedule | Storage policy per role |
| Network config | VLAN, bonding/teaming, MTU, offload settings | Network policy per role |
| Boot config | Boot device order, UEFI vs legacy, Secure Boot state | Compliance policy |

#### 5b. Compliance Flow

```
Site Manager pushes golden baseline per device model/role
    ↓
Agent polls current config via Redfish (BIOS attributes, BMC config)
    ↓
Agent compares current vs golden → produces COMPLIANT / DRIFTED / UNKNOWN verdict
    ↓
DRIFTED findings reported to Site Manager with exact delta
    ↓
(R3) Site Manager may authorize agent to remediate drift automatically
```

**R-AGENT-14.** Configuration comparison must be idempotent and must not modify any setting during the comparison phase.
**R-AGENT-15.** Drift detection must distinguish "intentionally different" (exception in policy) from "unintentionally changed" (actual drift).
**R-AGENT-16.** Configuration collection is a complete snapshot — not sampled — and must be repeatable to detect changes between polls.

---

### Capability 6: Firmware Inventory and Compliance

**Release: R2 (inventory + CVE flagging), R3 (staged updates)**

Every device has 10-20 firmware components (BIOS, BMC, NIC, RAID controller, drive firmware, PSU firmware, backplane, CPLD). Knowing what's running across the fleet and whether any of it is vulnerable is a capability no one has in real-time today.

#### 6a. Firmware Inventory (R2)

| Component | Data Collected | Source |
|-----------|---------------|--------|
| BIOS | Version, release date, vendor | Redfish Systems |
| BMC (iDRAC/iLO) | Version, release date, build | Redfish Managers |
| NIC | Firmware version, driver version | Redfish NetworkAdapters |
| RAID controller | Firmware version, driver version | Redfish Storage/Controllers |
| Drive | Firmware version, model, serial, SMART | Redfish Storage/Drives |
| PSU | Firmware version, model, wattage | Redfish Power/PowerSupplies |
| GPU (when present) | Firmware/VBIOS version, driver | Vendor extension |

**R-AGENT-17.** Firmware inventory must be collected at agent startup and re-polled on a configurable schedule (default: daily).
**R-AGENT-18.** Version strings must be normalized across vendors for comparison (e.g., Dell "2.83.83.83" vs HPE "2.72" → comparable version objects).

#### 6b. Firmware Compliance (R2)

- Site Manager maintains a firmware compliance matrix: minimum required versions, known-bad versions (CVE-affected), target versions
- Agent compares local inventory against the matrix → produces `CURRENT`, `UPDATE_AVAILABLE`, `VULNERABLE`, `UNKNOWN` verdicts
- `VULNERABLE` verdict is `CRITICAL` severity and generates an immediate alert

#### 6c. Firmware Update Execution (R3)

- Site Manager stages firmware packages and pushes update instructions to agents
- Agent applies updates via Redfish UpdateService (scheduled update, immediate, or on-next-reboot)
- Agent verifies post-update version matches expected version
- Update execution follows the same staged fleet model as agent upgrades (health gates between waves, automatic rollback)

**R-AGENT-19.** The agent must never self-update its own firmware update capability. Firmware updates are a commanded operation from Site Manager with explicit authorization.
**R-AGENT-20.** Failed firmware updates must be reported with full diagnostic context (error codes, pre/post state) and must not be retried automatically.

---

### Capability 7: Asset Discovery and Inventory

**Release: R2 (hardware inventory), R3 (warranty + lifecycle)**

The agent produces CMDB-grade asset data collected live from the hardware itself — not a stale spreadsheet that's wrong by the time it's finished.

#### 7a. Hardware Inventory (R2)

| Category | Data Points |
|----------|------------|
| System identity | Service tag, serial number, model, manufacturer, SKU, UUID |
| CPU | Model, core count, thread count, speed, microcode version, socket |
| Memory | DIMM slot, capacity, speed, type (DDR4/5), manufacturer, part number, serial |
| Storage | Drive slot, capacity, type (SSD/HDD/NVMe), model, serial, interface, media type |
| Network | NIC model, port count, speed capability, MAC addresses, firmware, PCI slot |
| GPU | Model, VRAM, serial, PCI slot, TDP |
| PSU | Model, wattage, input voltage, redundancy status, serial |
| Chassis | Form factor, rack units, asset tag, physical location (if set) |
| PCIe | Slot inventory, populated vs empty, device in each slot, link width/speed |

**R-AGENT-21.** Inventory is collected at agent startup and on-change (Redfish events for hot-add/remove). Full re-inventory on configurable schedule (default: weekly).
**R-AGENT-22.** Inventory data is reported to Site Manager in normalized schema. Site Manager is the CMDB, not the agent.

#### 7b. Warranty and Lifecycle (R3)

| Data | Source | Value |
|------|--------|-------|
| Warranty status | Vendor API lookup via Site Manager (Dell TechDirect, HPE Warranty API) | "This server's warranty expires in 30 days" |
| End-of-life/end-of-support | Vendor product lifecycle databases | "iDRAC9 reaches end-of-support in Q2 2027" |
| Part number cross-reference | Vendor part catalog | "Replacement drive for slot 3: Dell P/N 400-BKPZ" |

**R-AGENT-23.** Warranty and lifecycle lookups are performed by Site Manager (requires internet), not the agent. Agent provides the serial/service tag; Site Manager returns the enriched data.

---

### Capability 8: OS-Level Correlation

**Release: R2 (basic OS signals), R3 (application-aware)**

Redfish gives you the hardware view. But a disk SMART warning means nothing until you know which LUN, filesystem, and application sits on that drive. The agent, running on the OS, has both views.

#### 8a. OS Hardware Signals (R2)

| Source | Data | Value |
|--------|------|-------|
| `mcelog` / `rasdaemon` | Machine check exceptions, memory errors with physical address mapping | Maps ECC errors to specific DIMM + physical page → ties to Redfish DIMM identity |
| `dmesg` / kernel ring buffer | PCIe AER errors, NVMe errors, disk I/O errors, driver failures | Real-time hardware fault signals the OS sees |
| `smartctl` (OS-side) | Drive SMART attributes at higher polling frequency than Redfish | Complementary to Redfish SMART, sometimes more current |
| `/sys/class/thermal` | CPU thermal throttling events from OS perspective | Correlate with Redfish thermal → "is the server actually throttling workloads?" |
| `/proc/interrupts`, `/proc/softirqs` | Interrupt storm detection, NIC interrupt distribution | Detect hardware causing kernel instability |
| `lspci` / `lsblk` / `ip link` | Device-to-OS mapping: which PCIe device → which block device → which mount point | The bridge from hardware identity to application impact |

#### 8b. Hardware-to-Application Mapping (R3)

The killer capability: "Drive in slot 5 is failing. It is `/dev/sdb`. That is mounted as `/data/postgres`. Your PostgreSQL database will lose its primary storage within approximately 72 hours."

| Mapping | How |
|---------|-----|
| Redfish Drive → OS block device | Match by serial number or PCI address |
| Block device → filesystem / LVM / RAID | `lsblk`, `pvs`, `lvs`, `mdstat` |
| Filesystem → running process | `/proc/*/maps`, `fuser`, `lsof` |
| Process → service / application | `systemctl`, cgroup membership, container labels |
| NIC port → OS interface → IP → service | `ip link`, `ss`, service config |

**R-AGENT-24.** OS-level collection must respect the P5 principle: never destabilize the host. All OS probes must be read-only, non-blocking, and bounded in CPU/memory/IO.
**R-AGENT-25.** The hardware-to-application map is cached and rebuilt on change events (mount, process start/stop, device hot-add), not polled continuously.

---

### Capability 9: Credential Rotation

**Release: R2 (rotation execution), R3 (multi-device-class)**

Already specified in detail in [Document 3](03-credential-rotation.md). This section covers the agent's role specifically.

#### Agent's Role in the Credential Flow

```
Site Manager Credential Proxy generates new credential
    ↓
Signed instruction sent to agent with: new credential, target (BMC), method (Redfish AccountService), rollback credential
    ↓
Agent applies new credential to BMC via Redfish
    ↓
Agent verifies new credential works (test authentication)
    ↓
Agent reports success/failure to Site Manager
    ↓
(On failure) Agent rolls back to previous credential using rollback credential
```

**R2 scope:** Server BMC credentials (iDRAC, iLO) via Redfish AccountService.
**R3 scope:** Network switch credentials (SSH/NETCONF), storage controller credentials (vendor API).

**R-AGENT-26.** Agent never stores credentials long-term. JIT from Site Manager with 15-minute cached TTL. R1 interim: local encrypted config (AES-256).
**R-AGENT-27.** Credential rotation is blue-green: create new account, verify it works, then disable old account. Never mutate the only account.
**R-AGENT-28.** Failed rotation is a `CRITICAL` event that immediately escalates to Site Manager. The agent does not retry credential changes autonomously.

---

### Capability 10: Audit Trail

**Release: R2 (action audit), R3 (compliance-grade)**

Every action the agent takes — or refuses to take — must be recorded with enough detail for compliance review and incident forensics.

#### 10a. What is Audited

| Event Type | Data Recorded |
|------------|--------------|
| Action executed | What, when, who authorized, pre-state, post-state, outcome, duration |
| Action refused | What was requested, why refused (policy, rate limit, expiry, pre-condition failure) |
| Action failed | What was attempted, error detail, whether rollback succeeded |
| Configuration change detected | What changed, from what, to what, when first detected |
| Credential operation | Rotation attempted, target, outcome (never the credential itself) |
| Firmware update | Component, from-version, to-version, outcome |
| Skill evaluation | Which skill, what evidence, what verdict, what confidence |
| Agent lifecycle | Start, stop, crash, restart, upgrade, config change |

#### 10b. Audit Properties

**R-AGENT-29.** Audit records are append-only on the agent. The agent cannot delete or modify its own audit trail.
**R-AGENT-30.** Audit records are forwarded to Site Manager in near-real-time. If Site Manager is unreachable, records are buffered locally. Audit records must never be shed under buffer pressure (telemetry may be shed; audit may not).
**R-AGENT-31.** Each audit record is cryptographically chained (hash of previous record included) to detect tampering or gaps.
**R-AGENT-32.** Audit records include a monotonic sequence number per agent to detect missing records on the receiving end.

---

## 4. Release Mapping

### R1 — Diagnostic Foundation (current, 8-10 weeks)

| Capability | Scope | Deliverable |
|------------|-------|-------------|
| 1. Diagnosis | Telemetry + Redfish logs + skill evaluation | Python CLI agent polling 4 simulated devices (Dell iDRAC9/10, HPE iLO5/6). 5 fault types × 2 vendors. |
| 2. Predictive Trending | Linear regression on sensor time-series | `TRENDING` verdicts with projected time-to-threshold |
| 3. Peer Heartbeat | Discovery + liveness only | Agents find each other, detect when a neighbor goes silent |
| — | Terminal UI | Python `rich` library, live dashboard of fleet health |
| — | `harken demo` | One-command 60-second automated showcase |
| — | Redfish Mock Simulator | Simulates 4 device types with injectable faults |

### R2 — Intelligence Layer (after R1)

| Capability | Scope | Deliverable |
|------------|-------|-------------|
| 1. Diagnosis | + OS-level signals (syslog, dmesg, mcelog) | Hardware-to-OS correlation |
| 3. Peer Heartbeat | + Quorum, corroboration, incident ownership | Mesh intelligence: distinguish device-down vs agent-crash vs partition |
| 4. Remediation | Basic action allow-list | LED blink, SEL clear, BMC reset, power cycle (with SM approval) |
| 5. Configuration Compliance | Drift detection | Compare current config vs golden baseline, report drift |
| 6. Firmware Inventory | Inventory + CVE flagging | Collect all firmware versions, flag known-vulnerable |
| 7. Asset Inventory | Hardware inventory | CMDB-grade live asset data from every device |
| 8. OS Correlation | Basic OS signals | Syslog, dmesg, mcelog integration |
| 9. Credential Rotation | Server BMC credentials | Redfish AccountService rotation with blue-green model |
| 10. Audit Trail | Action audit | Every action/refusal recorded, forwarded to SM |
| — | LLM Explain | Site Manager uses LLM to generate root-cause analysis from agent evidence |
| — | Skill Generation | Site Manager LLM generates new skills from observed patterns |

### R3 — Full Autonomy (after R2)

| Capability | Scope | Deliverable |
|------------|-------|-------------|
| 3. Peer Heartbeat | Full mesh | Suspicion state exchange, threshold-triggered claims, cross-peer trending |
| 4. Remediation | Multi-step playbooks | Disk replacement prep, thermal mitigation, memory remediation, NIC failover |
| 5. Configuration Compliance | + Auto-remediation | Agent can fix drift with SM authorization |
| 6. Firmware | + Staged update execution | Fleet-wide firmware updates with health gates and rollback |
| 7. Asset Inventory | + Warranty + lifecycle | Vendor API integration for warranty, EOL, part cross-reference |
| 8. OS Correlation | Application-aware | Hardware → OS device → filesystem → process → service mapping |
| 9. Credential Rotation | + Switches (SSH) + storage | Multi-device-class credential rotation |
| 10. Audit Trail | Compliance-grade | Cryptographic chaining, tamper detection, compliance reporting |
| — | Network device management | Switch/router monitoring via gRPC/gNMI/SSH |
| — | Auto-generated skills | LLM generates skills from incident patterns, pushes to agents |

### R4 — Fleet Intelligence (after R3)

| Capability | Scope | Deliverable |
|------------|-------|-------------|
| — | Storage management | OneFS, SAN/NAS monitoring via vendor APIs |
| — | Cross-site correlation | Cluster Manager correlates patterns across sites |
| — | Predictive procurement | "Based on trending, you need 12 replacement drives in the next 90 days" |
| — | Capacity planning | Resource utilization trending for planning |
| — | Self-optimizing skills | Skills evolve based on outcome data (did the recommended action actually fix it?) |

---

## 5. Agent State Machine

The agent operates as an explicit state machine with defined crash recovery.

```
                    ┌──────────┐
                    │  BOOTING │
                    └────┬─────┘
                         │ load config, restore checkpoint
                         ▼
                    ┌──────────┐
              ┌────►│ OBSERVING│◄────────────────────┐
              │     └────┬─────┘                     │
              │          │ skill condition met        │
              │          ▼                            │
              │     ┌──────────┐                     │
              │     │EVALUATING│                     │
              │     └────┬─────┘                     │
              │          │ verdict produced           │
              │          ▼                            │
              │     ┌──────────┐    no action needed  │
              │     │ DECIDING ├─────────────────────►│
              │     └────┬─────┘                     │
              │          │ action required            │
              │          ▼                            │
              │     ┌──────────────┐                  │
              │     │AWAITING_AUTH │ (request SM)     │
              │     └────┬─────┘                     │
              │          │ authorized / locally allowed│
              │          ▼                            │
              │     ┌──────────┐                     │
              │     │  ACTING  │                     │
              │     └────┬─────┘                     │
              │          │ action complete/failed      │
              │          ▼                            │
              │     ┌──────────┐                     │
              └─────┤REPORTING │                     │
                    └────┬─────┘                     │
                         │ report sent               │
                         └───────────────────────────┘
```

**On crash:** Agent restarts → loads last checkpoint → enters `OBSERVING`. Any in-flight action is treated as `UNKNOWN` outcome and reported to Site Manager for reconciliation.

---

## 6. Open Questions (Capability-Specific)

| # | Question | Affects | Decision Gate |
|---|----------|---------|---------------|
| Q1 | What OS distributions must the agent support? RHEL, Ubuntu, SLES? Container-only hosts (CoreOS)? | All capabilities | Before R1 code start |
| Q2 | Does the design partner have iDRAC Datacenter licenses? Affects telemetry streaming availability. | Cap 1 (Diagnosis) | R1 Week 1 |
| Q3 | iDRAC9 vs iDRAC10 Redfish schema differences for 5 fault types? | Cap 1 (Diagnosis) | R1 Week 1 |
| Q4 | What existing CMDB does the design partner use? How stale is it? | Cap 7 (Asset Inventory) | Before R2 |
| Q5 | What compliance frameworks apply (SOC2, ISO27001, PCI-DSS)? Shapes audit requirements. | Cap 10 (Audit) | Before R3 |
| Q6 | Does the design partner run workloads in containers, VMs, or bare metal? | Cap 8 (OS Correlation) | Before R2 |
| Q7 | What is the approval workflow today for hardware actions? Ticketing system? | Cap 4 (Remediation) | Before R2 |

---

## 7. What This Replaces

When all capabilities are delivered, the following tools become redundant for hardware operations:

| Tool Category | Examples | Replaced By |
|---------------|----------|-------------|
| Vendor BMC consoles | iDRAC web UI, iLO web UI | Cap 1 (Diagnosis) + Cap 4 (Remediation) |
| Vendor fleet managers | Dell OpenManage Enterprise, HPE OneView | Site Manager + Cap 7 (Asset Inventory) |
| Infrastructure monitoring | SolarWinds, Nagios, Zabbix, PRTG | Cap 1 + Cap 2 (Trending) + Cap 8 (OS Correlation) |
| Configuration management | Vendor-specific config tools | Cap 5 (Configuration Compliance) |
| Firmware management | Dell Repository Manager, HPE SUM | Cap 6 (Firmware) |
| Asset management | Manual spreadsheets, ServiceNow CMDB (manual entry) | Cap 7 (Asset Inventory) |
| Credential management | Manual rotation, CyberArk/HashiCorp (partial) | Cap 9 (Credential Rotation) |
| Audit/compliance | Manual log collection, SIEM (partial) | Cap 10 (Audit Trail) |

**The value proposition in one line:** Every tool above requires a human to interpret its output and decide what to do. HarkenIQ observes, learns, reasons, and acts — the device operates itself.
