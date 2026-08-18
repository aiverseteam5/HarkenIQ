# Document 9: Demo Scenario Script

**Purpose:** Second-by-second specification of the `harken demo` 60-second automated showcase.
**Scope:** Progressive failure cascade demonstrating diagnosis, trending, peer witness, action pipeline.
**Status:** Draft.

---

## 1. Overview

`harken demo` is a scripted 60-second showcase that runs against the built-in Redfish mock simulator. It demonstrates all R1 capabilities in a progressive failure cascade: a server begins healthy, faults are injected one at a time with increasing severity, and HarkenIQ detects, predicts, and responds to each. Two simulated peers demonstrate the heartbeat and witness model.

The demo is the R1 centerpiece deliverable. It must be reproducible, self-contained, and require no real hardware.

---

## 2. Pre-Conditions

```bash
harken demo [--speed 1.0] [--scenario all]
```

### 2.1 Simulated Environment

| Component | Configuration |
|-----------|--------------|
| Primary device | Dell PowerEdge R750 (iDRAC9) -- "rack-12-server-04" |
| Mock BMC | localhost:8443 (mock simulator) |
| Peer 1 | "rack-12-server-03" (simulated heartbeat) |
| Peer 2 | "rack-12-server-05" (simulated heartbeat) |
| Baselines | **Pre-seeded** from checkpoint (see §2.3) |

### 2.3 Demo Baseline Pre-Seeding

**Problem:** Trending verdicts require 60+ samples (~1 hour at 60s polling), but the demo runs in 60 seconds. Baselines cannot be learned in real-time during the demo.

**Solution:** The demo pre-seeds the agent's checkpoint database with 24 hours of synthetic healthy baseline data before the demo starts. This gives all sensors confidence = 1.0 from t=0, enabling both threshold verdicts and trending verdicts immediately.

Pre-seeding procedure (handled by `harken demo` automatically):
1. Create a temporary checkpoint.db with synthetic baselines for all sensors
2. Each baseline: 1440 samples (24h at 60s), values within healthy range with realistic noise
3. Trending regression state initialized from the synthetic samples
4. Agent loads checkpoint at startup, enters OBSERVING with full confidence immediately

**Demo-mode config overrides:**
```yaml
# Applied automatically by harken demo (not user-configurable)
baseline:
  min_samples: 5              # Reduced from 60 — trending triggers after 5 new samples
trending:
  min_samples: 5              # Reduced from 60 — allows trending within demo window
  r_squared_min: 0.3          # Reduced from 0.5 — fewer samples = noisier fit
polling:
  sensor_interval: 2          # 2 seconds instead of 60 — compressed for demo
```

With these overrides and pre-seeded baselines:
- Fan RPM decline injected at t=5 produces 5 samples by t=15 → TRENDING verdict at t=15
- Disk SSD life set at t=15 produces an immediate WARNING (threshold-based, no trending needed)
- All trending projections use the pre-seeded 24h baseline + new samples for regression
| Site Manager | Not connected (standalone mode) |
| Skills | All 5 default skills loaded |
| Baseline | Pre-seeded with 24 hours of healthy data (confidence = 1.0) |

### 2.2 Initial Healthy State

All subsystems healthy at t=0:

| Subsystem | State |
|-----------|-------|
| Fans | 8 fans, all OK, 9200-10400 RPM |
| Disks | 4 disks, all OK, SSD life 85-98% |
| Memory | 16x 32GB DDR4, 0 ECC errors |
| PSUs | 2x 1400W, redundant, 186W draw |
| Thermal | Inlet 22°C, CPU1 54°C, CPU2 52°C, exhaust 38°C |
| Peers | 2 alive, heartbeats received |

---

## 3. Timeline

Demo clock runs at `--speed` multiplier (default 1.0 = real-time). At `--speed 10`, the demo completes in 6 seconds. Internally, the simulator injects faults at scripted time points regardless of speed.

### Phase 1: Healthy Baseline (t=0 to t=5)

**t=0:** Demo starts. TUI appears.

```
┌─ HarkenIQ Demo ──────────────────── rack-12-server-04 ─┐
│ Device: Dell PowerEdge R750 (iDRAC9)                    │
│ State:  OBSERVING          Mode: Demo (60s)             │
├─────────────────────────────────────────────────────────┤
│ SUBSYSTEM       STATUS    DETAIL                        │
│ Fan Health      ✓ OK      8/8 fans, 9200-10400 RPM     │
│ Disk Health     ✓ OK      4 disks, SSD life 85-98%     │
│ Memory Health   ✓ OK      512 GB, 0 ECC errors         │
│ PSU Health      ✓ OK      2x 1400W, redundant, 186W    │
│ Thermal         ✓ OK      Inlet 22°C / Exhaust 38°C    │
├─────────────────────────────────────────────────────────┤
│ PEERS                                                   │
│ rack-12-srv-03  ✓ alive   1s ago                        │
│ rack-12-srv-05  ✓ alive   2s ago                        │
├─────────────────────────────────────────────────────────┤
│ PENDING ACTIONS  (none)                                 │
├─────────────────────────────────────────────────────────┤
│ EVENTS                                                  │
│ 00:00  ✓ All subsystems healthy — baseline established  │
└─────────────────────────────────────────────────────────┘
```

**t=2:** First poll completes. All verdicts HEALTHY. Event: "Poll 1: all healthy."

**t=5:** Narration line appears at bottom: *"Watch: a fan starts slowing down..."*

---

### Phase 2: Fan Degradation — TRENDING (t=5 to t=15)

**t=5:** Mock simulator begins declining Fan1A RPM from 9800 → 9600 → 9400 → 9200...

**t=7:** Poll detects declining trend. TUI updates:

```
│ Fan Health      ↘ TREND   Fan1A declining at -200 RPM/hr  │
```

Event: `00:07  ↘ Fan1A speed declining at -200 RPM/hr`

**t=10:** Trend projection calculated:

Event: `00:10  ↘ Fan1A projected to reach critical threshold (480 RPM) in 46 hours`

**t=12:** Fan1A drops further. RPM now 8800 (still above critical but visibly declining in TUI).

**t=15:** Narration: *"Fan is still running but HarkenIQ caught the decline 46 hours before failure. Now a disk..."*

---

### Phase 3: Disk SMART Alert — WARNING (t=15 to t=25)

**t=15:** Mock simulator sets Disk.Bay.2 SSD life to 18% and triggers SMART predictive failure.

**t=17:** Poll detects disk warning. TUI updates:

```
│ Disk Health     ⚠ WARN    Bay 2: SSD life 18% (SMART alert)  │
```

Event: `00:17  ⚠ Disk Bay 2 (SAMSUNG MZ7LH960) SMART predictive failure — SSD life 18%`

**t=18:** Trending shows SSD wear rate:

Event: `00:18  ↘ Disk Bay 2 SSD life declining at -2.1%/month — replacement in ~8.5 months`

**t=19:** Action proposed — LED identification blink. Enters pending queue:

```
│ PENDING ACTIONS                                           │
│ [1] IDENTIFY_LED on Disk.Bay.2 — SSD life 18%  [a/d]    │
```

Event: `00:19  ⚡ Action proposed: blink LED on Disk.Bay.2 for field tech identification`

**t=22:** Narration: *"SSD is wearing out. LED blink proposed so a tech can find the drive. Press 'a' to approve. Now a peer goes down..."*

**t=25:** (If operator pressed 'a', LED blink executes and audit log records it. If not, action stays in queue.)

---

### Phase 4: Peer Goes Down — Witness Evidence (t=25 to t=35)

**t=25:** Mock stops sending heartbeats from rack-12-server-03.

**t=28:** Heartbeat timeout (3 consecutive misses). TUI updates:

```
│ PEERS                                                     │
│ rack-12-srv-03  ✗ UNRESPONSIVE  3s ago                   │
│ rack-12-srv-05  ✓ alive         1s ago                    │
```

Event: `00:28  ⚠ Peer rack-12-server-03 unresponsive`

**t=29:** Pre-failure evidence displayed:

Event: `00:29  📋 Pre-failure evidence from rack-12-server-03: fan=OK disk=OK mem=OK psu=OK thermal=OK`

**t=30:** Narration: *"Peer went down. HarkenIQ retained its last known health status — the witness model. If this were a real crash, we'd know the hardware was healthy before the OS died. Now a PSU failure..."*

---

### Phase 5: PSU Failure — CRITICAL + Action (t=35 to t=50)

**t=35:** Mock simulator sets PSU 2 to Absent, redundancy status to Degraded.

**t=37:** Poll detects PSU failure. TUI updates with color change (red):

```
│ PSU Health      ✗ CRIT    PS2 absent — redundancy LOST    │
```

Event: `00:37  ✗ PSU PS2 has been removed — redundancy lost`

**t=38:** System power metrics show single PSU carrying full load:

Event: `00:38  ⚠ PSU PS1 now carrying full load: 186W / 1400W capacity`

**t=39:** Action proposed — collect diagnostic logs:

```
│ PENDING ACTIONS                                           │
│ [1] IDENTIFY_LED on Disk.Bay.2 — SSD life 18%  [a/d]    │
│ [2] COLLECT_DIAGNOSTICS — PSU failure evidence   [a/d]   │
```

Event: `00:39  ⚡ Action proposed: collect server diagnostic logs (PSU failure)`

**t=45:** Narration: *"PSU failure detected instantly. Redundancy is gone — one more PSU failure and the server goes dark. Diagnostic log collection proposed. Now watch the thermal cascade..."*

---

### Phase 6: Thermal Cascade — Cross-Subsystem (t=50 to t=55)

**t=50:** The fan degradation from Phase 2 causes reduced cooling. Mock simulator raises inlet temperature from 22°C → 28°C and CPU1 from 54°C → 62°C (above the warning threshold of 42°C for inlet).

**t=52:** Poll detects thermal warning. TUI updates:

```
│ Fan Health      ↘ TREND   Fan1A declining at -200 RPM/hr  │
│ Thermal         ⚠ WARN    Inlet 28°C (⚠ rising)          │
```

Event: `00:52  ⚠ Sensor Inlet Temp at 28°C — rising, correlated with Fan1A degradation`

**t=53:** Cross-subsystem correlation note:

Event: `00:53  🔗 Cross-subsystem: thermal rise correlates with Fan1A RPM decline (r=0.92)`

**t=54:** Action proposed — fan profile reset:

```
│ PENDING ACTIONS                                           │
│ [1] IDENTIFY_LED on Disk.Bay.2           [a/d]           │
│ [2] COLLECT_DIAGNOSTICS — PSU failure    [a/d]           │
│ [3] FAN_RESET — thermal rising           [a/d]           │
```

**t=55:** Narration: *"The fan degradation is now causing temperatures to rise. HarkenIQ connected the dots across subsystems. Fan reset proposed to restore default thermal profile."*

---

### Phase 7: Summary Dashboard (t=55 to t=60)

**t=55:** TUI transitions to summary view:

```
┌─ HarkenIQ Demo Summary ─────────── rack-12-server-04 ──┐
│                                                          │
│  VERDICT SUMMARY                                         │
│  ═══════════════                                         │
│                                                          │
│  ✗ CRITICAL  PSU PS2 absent — redundancy lost            │
│  ⚠ WARNING   Disk Bay 2 SSD life 18% (SMART alert)      │
│  ⚠ WARNING   Inlet Temp 28°C — rising (fan correlation)  │
│  ↘ TRENDING  Fan1A declining at -200 RPM/hr              │
│              → critical in 46 hours                      │
│  ↘ TRENDING  Disk Bay 2 SSD life -2.1%/month             │
│              → replacement in 8.5 months                 │
│                                                          │
│  PEER WITNESS                                            │
│  ════════════                                            │
│  rack-12-srv-03: UNRESPONSIVE                            │
│    Last known: all subsystems healthy                    │
│  rack-12-srv-05: alive                                   │
│                                                          │
│  ACTIONS PROPOSED: 3                                     │
│  [1] Blink LED on Disk.Bay.2 (awaiting approval)        │
│  [2] Collect diagnostic logs (awaiting approval)         │
│  [3] Reset fan thermal profile (awaiting approval)       │
│                                                          │
│  CAPABILITIES DEMONSTRATED                               │
│  ════════════════════════                                │
│  ✓ Real-time fault detection (fan, disk, PSU, thermal)  │
│  ✓ Predictive trending (46-hour fan failure prediction)  │
│  ✓ Cross-subsystem correlation (fan → thermal)           │
│  ✓ Peer heartbeat and witness evidence                   │
│  ✓ Gated action pipeline (3 actions awaiting approval)   │
│  ✓ Cross-vendor normalization (Dell iDRAC9)              │
│                                                          │
│  Demo complete. Press any key to exit.                   │
└──────────────────────────────────────────────────────────┘
```

**t=60:** Demo ends. Exit code 0 if all verdicts matched expected outcomes.

---

## 4. Mock Simulator Fault Injection Sequence

The demo controller issues these fault injection commands to the mock simulator:

```python
demo_sequence = [
    # (time_offset_seconds, fault_injection)
    (5,  {"device": "dell-r750", "fault_type": "fan", "target": "Fan1A",
           "params": {"mode": "gradual_decline", "start_rpm": 9800, "rate": -200, "interval": 2}}),
    (15, {"device": "dell-r750", "fault_type": "disk", "target": "Disk.Bay.2",
           "params": {"health": "Warning", "life_left_pct": 18, "smart_alert": True}}),
    (25, {"device": "peer", "target": "rack-12-server-03",
           "params": {"action": "stop_heartbeat"}}),
    (35, {"device": "dell-r750", "fault_type": "psu", "target": "PS2",
           "params": {"state": "Absent", "redundancy_health": "Warning"}}),
    (50, {"device": "dell-r750", "fault_type": "thermal", "target": "Inlet",
           "params": {"reading_c": 28, "cpu1_reading_c": 62}}),
]
```

---

## 5. Time Compression

The `--speed` flag compresses the demo timeline:

| Speed | Duration | Use Case |
|-------|----------|----------|
| 1.0 | 60 seconds | Live presentation |
| 2.0 | 30 seconds | Quick demo |
| 5.0 | 12 seconds | CI/test verification |
| 10.0 | 6 seconds | Automated testing |

At speed > 1.0:
- Poll intervals are compressed proportionally
- Fault injection timing is compressed
- Narration text still appears but is not delayed
- TUI update rate stays at ~10 FPS regardless of speed

---

## 6. Acceptance Criteria

| Criterion | Test |
|-----------|------|
| Demo completes without error | Exit code 0 |
| All 5 fault types are demonstrated | Verdicts for fan, disk, memory (baseline only), PSU, thermal |
| Trending verdict appears | Fan RPM trending with time-to-threshold projection |
| Peer unresponsive detected | rack-12-server-03 marked unresponsive within 30s of heartbeat stop |
| Pre-failure evidence shown | Last known health of unresponsive peer displayed |
| Actions proposed | At least 2 actions in pending queue |
| Cross-subsystem correlation | Fan degradation linked to thermal rise |
| Summary dashboard shown | All verdicts, peer status, and actions listed |
| Duration within tolerance | Completes within 5% of expected duration (63s at speed 1.0) |
| Reproducible | Same output on repeated runs (deterministic fault injection timing) |

---

## 7. Individual Scenario Mode

`harken demo --scenario <name>` runs a single fault scenario:

| Scenario | Duration | Description |
|----------|----------|-------------|
| `fan-failure` | 15s | Fan RPM decline → TRENDING → CRITICAL |
| `disk-smart` | 15s | SSD wear → WARNING → LED blink proposed |
| `memory-ecc` | 15s | ECC error count rising → WARNING → TRENDING |
| `psu-redundancy` | 15s | PSU removal → CRITICAL → diagnostic collection |
| `thermal-warning` | 15s | Temperature rise → WARNING → fan reset proposed |
| `peer-witness` | 15s | Peer goes down → evidence retained |
| `all` (default) | 60s | Full progressive cascade |

---

## 8. Narration Text

The demo includes brief narration lines at the bottom of the TUI. These are for live presentation context -- they explain what's happening to a non-technical audience.

| Time | Narration |
|------|-----------|
| t=0 | "All systems healthy. Baselines established. Watching..." |
| t=5 | "Watch: a fan starts slowing down..." |
| t=15 | "Fan decline detected 46 hours before failure. Now a disk..." |
| t=22 | "SSD wearing out. LED blink proposed for field tech. Now a peer..." |
| t=30 | "Peer down — last health snapshot retained. Now a PSU..." |
| t=45 | "PSU failed, redundancy gone. Diagnostics proposed. Thermal cascade..." |
| t=55 | "Fan degradation caused thermal rise. HarkenIQ connected the dots." |
| t=58 | "Demo complete. All detections, predictions, and actions in 60 seconds." |
