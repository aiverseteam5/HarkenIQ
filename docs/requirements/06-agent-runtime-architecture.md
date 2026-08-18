# Document 6: Agent Runtime Architecture

**Purpose:** Implementation-ready specification for the HarkenIQ agent process model, packaging, configuration, and deployment.
**Scope:** R1 Python CLI agent running on Dell PowerEdge and HPE ProLiant servers.
**Status:** Draft. Updated 2026-08-18 with decisions D6-D18.

---

## 1. Overview

The HarkenIQ agent is a Python process that runs on the OS of each managed server. It polls the local BMC (iDRAC/iLO) via Redfish HTTPS, evaluates fault-detection skills against the telemetry, exchanges heartbeats with peer agents, and reports results to the Site Manager.

**R1 deliverables:**
- `harken` CLI with subcommands (agent, demo, status, diagnose)
- systemd service unit for production deployment
- Terminal UI dashboard (Python `rich` library)
- Redfish mock simulator for development and testing

---

## 2. Process Model

### 2.1 Runtime

| Property | Value |
|----------|-------|
| Language | Python 3.11+ |
| Execution model | Single-process, async (`asyncio`) event loop |
| Concurrency | `asyncio` tasks for polling, heartbeat, and reporting |
| Process type | Long-running daemon (systemd) or foreground CLI |

### 2.2 Main Loop

The agent runs a single `asyncio` event loop with three concurrent task groups:

```
┌─────────────────────────────────────────────────┐
│                  asyncio event loop              │
│                                                  │
│  ┌──────────────┐  ┌──────────────┐  ┌────────┐ │
│  │  Poller Task  │  │ Heartbeat    │  │ Report │ │
│  │              │  │ Task         │  │ Task   │ │
│  │ 60s: sensors │  │ 10s: UDP     │  │ gRPC   │ │
│  │ 300s: logs   │  │ send/recv    │  │ stream │ │
│  │ 300s: inv.   │  │ peer liveness│  │ to SM  │ │
│  └──────┬───────┘  └──────┬───────┘  └───┬────┘ │
│         │                 │              │      │
│         ▼                 ▼              ▼      │
│  ┌─────────────────────────────────────────────┐ │
│  │            Shared State (in-memory)          │ │
│  │  telemetry cache, baselines, peer table,     │ │
│  │  verdict history, checkpoint buffer          │ │
│  └─────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────┘
```

### 2.3 Task Intervals

| Task | Interval | Purpose |
|------|----------|---------|
| Sensor poll | 60s | Fan, thermal, PSU, memory, disk health |
| Log poll | 300s | SEL/IML entries since last check |
| Inventory poll | 300s | Drive/DIMM/PSU inventory changes |
| Skill evaluation | After each sensor poll | Run fault detection rules |
| Baseline update | After each sensor poll | Update per-device running statistics |
| Heartbeat send | 10s | UDP ping to each configured peer |
| Heartbeat check | 30s | Evaluate peer liveness (3 missed = unresponsive) |
| Checkpoint | 600s | Persist state to disk |
| Report to SM | On verdict change or 60s heartbeat | gRPC to Site Manager (stub in R1) |

---

## 3. Privilege and Security Model

### 3.1 Service User

```
User:  harkeniq
Group: harkeniq
Shell: /usr/sbin/nologin
Home:  /var/lib/harkeniq
```

No root required. BMC access is over HTTPS (network socket only). No raw IPMI, no kernel module access.

### 3.2 Capabilities

The agent process needs no Linux capabilities beyond a normal user. The systemd unit restricts the process further:

```ini
[Service]
User=harkeniq
Group=harkeniq

# Filesystem
StateDirectory=harkeniq
ConfigurationDirectory=harkeniq
LogsDirectory=harkeniq
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/var/lib/harkeniq /var/log/harkeniq
PrivateTmp=yes

# Security hardening
NoNewPrivileges=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
MemoryDenyWriteExecute=yes
```

### 3.3 BMC Credentials

| Release | Credential Model |
|---------|-----------------|
| R1 | Local encrypted config file (`/etc/harkeniq/credentials.enc`, AES-256-GCM, key derived from machine-id + agent secret) |
| R2+ | Site Manager Credential Proxy (agent never stores creds long-term, 15-min cached TTL) |

The credentials file stores BMC username/password only. The agent authenticates to the BMC using HTTP Basic Auth or Redfish session tokens.

```yaml
# /etc/harkeniq/credentials.enc (plaintext before encryption, for reference)
bmc:
  username: harkeniq-svc
  password: <service-account-password>
```

Encryption at rest:
- Algorithm: AES-256-GCM
- Key derivation: PBKDF2(machine-id || agent-secret, salt, 100000 iterations)
- `agent-secret` generated on first run, stored in `/var/lib/harkeniq/.agent-secret` (mode 0600)

---

## 4. Directory Layout

```
/etc/harkeniq/                    # Configuration (root:harkeniq 750)
├── config.yaml                   # Agent configuration
├── credentials.enc               # Encrypted BMC credentials
└── skills/                       # Skill definitions (YAML)
    ├── fan-health.yaml
    ├── disk-health.yaml
    ├── memory-health.yaml
    ├── psu-health.yaml
    └── thermal-health.yaml

/var/lib/harkeniq/                # Persistent state (harkeniq:harkeniq 750)
├── .agent-secret                 # Encryption key material (mode 0600)
├── agent-id                      # Stable agent identity (UUID, generated on first run)
├── checkpoint.db                 # SQLite database for state persistence
└── baselines/                    # Per-sensor baseline data
    └── {sensor-hash}.json

/var/log/harkeniq/                # Logs (harkeniq:harkeniq 750)
├── agent.log                     # Main agent log (JSON lines)
└── audit.log                     # Action audit trail (append-only, JSON lines)

/opt/harkeniq/                    # Application code (root:root 755)
├── bin/
│   └── harken                    # CLI entry point
├── lib/                          # Python packages (venv)
└── share/
    └── harkeniq.service          # systemd unit template
```

---

## 5. Configuration

### 5.1 Config File

```yaml
# /etc/harkeniq/config.yaml

agent:
  id: auto                        # "auto" = read from /var/lib/harkeniq/agent-id
  name: "rack-12-server-04"       # Human-readable name (optional)
  log_level: INFO                 # DEBUG, INFO, WARNING, ERROR

bmc:
  host: auto                      # "auto" = auto-detect sequence (see 5.2)
  port: 443
  verify_ssl: false               # Self-signed BMC certs are the norm
  session_timeout: 300            # Redfish session renewal (seconds)

polling:
  sensor_interval: 60             # Seconds between sensor polls
  log_interval: 300               # Seconds between log polls
  inventory_interval: 300         # Seconds between inventory polls

heartbeat:
  port: 5150                      # UDP port for peer heartbeat
  interval: 10                    # Seconds between heartbeat sends
  timeout_multiplier: 3           # Missed heartbeats before "unresponsive"

peers:                            # Configured peer list for R1
  - host: 10.0.1.101
    port: 5150
  - host: 10.0.1.102
    port: 5150

site_manager:
  host: ""                        # Empty = standalone mode (R1 default)
  port: 50051                     # gRPC port
  # --site-manager-ip flag overrides

skills:
  directory: /etc/harkeniq/skills
  evaluation_on_poll: true        # Evaluate skills after each sensor poll

checkpoint:
  interval: 600                   # Seconds between state checkpoints
  max_baseline_age_days: 30       # Discard baselines older than this

baseline:
  window_samples: 1440            # Ring buffer size (24h at 60s polling)
  min_samples: 60                 # Minimum before baseline trusted
  max_age_days: 30                # Discard baselines older than this
  critical_pause_samples: 5       # Wait N samples after CRITICAL recovery

trending:
  min_samples: 60                 # Minimum data points before trending verdict
  slope_threshold: 0.05           # Rate-of-change threshold for TRENDING
  r_squared_min: 0.5              # Minimum R² for trend significance
  max_projection_days: 90         # Don't project beyond this horizon

debounce:
  critical: [2, 3]                # 2 of last 3 polls for CRITICAL
  warning: [3, 5]                 # 3 of last 5 polls for WARNING
  recovery: [3, 3]                # 3 consecutive healthy for recovery

actions:
  enabled: true                   # Enable action pipeline (D6)
  approval_mode: queue            # "queue" = queue-based review (D16)
  allow_list:                     # R1 allowed actions (D17)
    - IDENTIFY_LED
    - COLLECT_DIAGNOSTICS
    - FAN_RESET
```

### 5.2 BMC Auto-Detection Sequence

When `bmc.host` is `auto`, the agent probes in order:

1. `https://169.254.0.1:443` -- Dell USB-NIC (iDRAC Direct)
2. `https://169.254.0.2:443` -- HPE USB-NIC
3. `https://127.0.0.1:443` -- localhost fallback
4. Fail with error: "BMC not found. Set bmc.host in config.yaml or use --bmc-ip"

Each probe: `GET /redfish/v1/` with 3-second timeout. First 200 response wins.

### 5.3 Configuration Precedence

CLI flags > environment variables > config file > defaults.

```
HARKENIQ_BMC_HOST=10.0.1.100     # env var
harken agent start --bmc-ip X    # CLI flag wins
```

---

## 6. CLI Interface

### 6.1 Command Structure

```
harken
├── agent
│   ├── start           # Start the agent (foreground or daemon via systemd)
│   ├── stop            # Stop the agent (sends SIGTERM)
│   ├── status          # Show agent state, uptime, peer table, last verdict
│   └── checkpoint      # Force an immediate state checkpoint
├── diagnose            # One-shot: poll BMC, evaluate skills, print results, exit
├── demo                # 60-second automated showcase (harken demo)
├── action
│   ├── list            # Show pending actions
│   ├── approve <id>    # Approve a pending action
│   └── deny <id>       # Deny a pending action
├── mock
│   ├── start           # Start Redfish mock simulator
│   ├── stop            # Stop mock simulator
│   └── status          # Show mock simulator status
├── peers
│   ├── list            # Show configured peers and their liveness
│   └── ping <host>     # Test connectivity to a specific peer
├── config
│   ├── show            # Print effective configuration
│   ├── validate        # Validate config file
│   └── init            # Interactive first-time setup (generate config.yaml)
├── bmc
│   ├── detect          # Run BMC auto-detection and print result
│   ├── test            # Test BMC connectivity and authentication
│   └── inventory       # Print BMC hardware inventory
├── skills
│   ├── list            # List installed skills
│   ├── test <skill>    # Dry-run a skill against current telemetry
│   └── validate        # Validate all skill YAML files
└── version             # Print version info
```

### 6.2 Key Commands

**`harken agent start`** -- Main entry point.

```
harken agent start [OPTIONS]

Options:
  --bmc-ip HOST          BMC IP address (overrides config/auto-detect)
  --bmc-user USER        BMC username (overrides encrypted config)
  --bmc-pass PASS        BMC password (overrides encrypted config)
  --site-manager-ip HOST Site Manager address
  --peers HOST,HOST,...  Comma-separated peer list
  --foreground           Run in foreground (default when not under systemd)
  --tui                  Enable terminal UI dashboard
  --log-level LEVEL      Override log level
```

**`harken demo`** -- 60-second automated showcase.

```
harken demo [OPTIONS]

Options:
  --mock                 Use built-in Redfish mock simulator (no real BMC needed)
  --scenario SCENARIO    Fault scenario to demonstrate (default: all)
                         Scenarios: fan-failure, disk-smart, memory-ecc,
                                    psu-redundancy, thermal-warning
  --speed MULTIPLIER     Time compression (default: 1.0, use 10.0 for fast demo)
```

**`harken diagnose`** -- One-shot diagnosis.

```
harken diagnose [OPTIONS]

Options:
  --bmc-ip HOST          BMC IP address
  --json                 Output as JSON (for scripting)
  --verbose              Include raw telemetry in output
```

Output format (terminal):
```
HarkenIQ Diagnosis — Dell PowerEdge R750 (iDRAC9)
═══════════════════════════════════════════════════

  Fan Health        ✓ OK     8 fans, all within range
  Disk Health       ⚠ WARNING  Disk.Bay.2: SSD life 18% remaining
  Memory Health     ✓ OK     512 GB (16x 32GB DDR4), 0 ECC errors
  PSU Health        ✓ OK     2x 1400W redundant, 186W draw
  Thermal Health    ✓ OK     Inlet 22°C (threshold 42°C)

  Trending:
    Disk.Bay.2 SSD wear: -2.1%/month → replacement in ~8.5 months

  Peers: 2 reachable (rack-12-server-03, rack-12-server-05)
  Site Manager: not configured (standalone mode)
```

### 6.3 Exit Codes (D10)

| Code | Meaning | When |
|------|---------|------|
| 0 | All subsystems HEALTHY | `harken diagnose` finds no faults |
| 1 | At least one WARNING verdict | Degraded but operational |
| 2 | At least one CRITICAL verdict | Hardware failure detected |
| 3 | UNKNOWN / error | BMC unreachable, sensor error, unexpected failure |
| 4 | Configuration error | Bad config YAML, invalid skill file, missing credentials |

Follows the Nagios/Icinga plugin convention for ITOM tool interoperability.

---

## 7. State Persistence

### 7.1 Checkpoint Database

SQLite database at `/var/lib/harkeniq/checkpoint.db`. Chosen for:
- Embedded (no external dependencies)
- ACID transactions (crash-safe)
- Single-file backup
- Python stdlib (`sqlite3` module)

### 7.2 Schema

```sql
-- Agent identity and metadata
CREATE TABLE agent_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL  -- ISO 8601
);

-- Latest sensor readings (one row per sensor)
CREATE TABLE sensor_readings (
    sensor_id TEXT PRIMARY KEY,      -- e.g. "fan:System Board Fan1A"
    sensor_type TEXT NOT NULL,        -- fan, thermal, disk, memory, psu
    reading_json TEXT NOT NULL,       -- normalized reading as JSON
    health TEXT NOT NULL,             -- OK, Warning, Critical
    collected_at TEXT NOT NULL        -- ISO 8601
);

-- Baseline statistics per sensor
CREATE TABLE baselines (
    sensor_id TEXT PRIMARY KEY,
    mean REAL NOT NULL,
    stddev REAL NOT NULL,
    min_val REAL NOT NULL,
    max_val REAL NOT NULL,
    sample_count INTEGER NOT NULL,
    first_sample_at TEXT NOT NULL,
    last_sample_at TEXT NOT NULL,
    samples_json TEXT NOT NULL        -- ring buffer of recent values for trending
);

-- Verdict history (append-only, pruned to last 1000 per sensor)
CREATE TABLE verdicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sensor_id TEXT NOT NULL,
    skill_name TEXT NOT NULL,
    verdict TEXT NOT NULL,            -- HEALTHY, WARNING, CRITICAL, TRENDING, UNKNOWN
    evidence_json TEXT NOT NULL,      -- readings that triggered this verdict
    produced_at TEXT NOT NULL,
    reported_to_sm BOOLEAN DEFAULT 0
);

-- Peer liveness table
CREATE TABLE peers (
    peer_id TEXT PRIMARY KEY,         -- peer agent UUID
    host TEXT NOT NULL,
    port INTEGER NOT NULL,
    last_heartbeat_at TEXT,           -- ISO 8601, null = never heard from
    state TEXT NOT NULL DEFAULT 'unknown'  -- alive, unresponsive, unknown
);

-- Event log cursor (for incremental log polling)
CREATE TABLE log_cursors (
    log_source TEXT PRIMARY KEY,      -- e.g. "sel", "lclog", "iml"
    last_entry_id TEXT NOT NULL,
    last_poll_at TEXT NOT NULL
);

-- Audit trail (append-only, never pruned)
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    authorization TEXT,               -- signed auth token (R2+)
    outcome TEXT NOT NULL,            -- success, failed, refused, unknown
    evidence_json TEXT,
    logged_at TEXT NOT NULL
);
```

### 7.3 Checkpoint Behavior

- Checkpoint runs every 600 seconds (configurable).
- Uses SQLite WAL mode for non-blocking reads during writes.
- On crash: agent restarts, loads last checkpoint, enters OBSERVING state. Any in-flight action is recorded as UNKNOWN and reported to Site Manager.
- Baselines survive restart (R-AGENT-2). Baselines older than `max_baseline_age_days` are pruned.
- Audit log is never pruned (append-only). Rotation handled externally if needed.

### 7.4 Flash Wear Protection (R-MD5)

The agent limits write frequency to protect server flash storage:
- Checkpoint writes: max every 600 seconds (configurable, minimum 300s)
- SQLite WAL mode batches writes
- Baseline updates: in-memory ring buffer, flushed only at checkpoint
- Audit log: buffered, flushed every 60 seconds or on action completion
- Total write budget: < 1 MB per checkpoint cycle under normal operation

---

## 8. Logging

### 8.1 Log Format

JSON Lines to `/var/log/harkeniq/agent.log`:

```json
{"ts": "2026-09-15T14:30:00.123Z", "level": "INFO", "component": "poller", "msg": "Sensor poll complete", "sensors": 42, "duration_ms": 1200}
{"ts": "2026-09-15T14:30:01.456Z", "level": "WARNING", "component": "skill", "msg": "Verdict change", "sensor": "Disk.Bay.2", "from": "HEALTHY", "to": "WARNING", "evidence": {"life_left_pct": 18}}
{"ts": "2026-09-15T14:30:10.789Z", "level": "INFO", "component": "heartbeat", "msg": "Peer alive", "peer": "rack-12-server-03", "rtt_ms": 2}
```

### 8.2 Log Levels

| Level | Usage |
|-------|-------|
| DEBUG | Raw Redfish responses, skill evaluation details |
| INFO | Poll completions, verdict changes, peer events, startup/shutdown |
| WARNING | Fault verdicts, peer unresponsive, BMC connection retry |
| ERROR | BMC unreachable, checkpoint failure, skill parse error |

### 8.3 Audit Log

Separate append-only log at `/var/log/harkeniq/audit.log` for all actions and authorization decisions. Format matches the `audit_log` table schema as JSON Lines.

### 8.4 Log Rotation

Handled by logrotate (shipped as `/etc/logrotate.d/harkeniq`):

```
/var/log/harkeniq/agent.log {
    daily
    rotate 14
    compress
    missingok
    notifempty
    postrotate
        systemctl reload harkeniq-agent 2>/dev/null || true
    endscript
}

/var/log/harkeniq/audit.log {
    monthly
    rotate 12
    compress
    missingok
    notifempty
}
```

---

## 9. Peer Heartbeat Protocol

### 9.1 Transport

UDP datagrams on port 5150 (configurable). UDP chosen for:
- Low overhead (10-second interval per peer)
- No connection state to manage
- Failure detection semantics match (missed packets = unresponsive)

### 9.2 Heartbeat Packet (D18: HMAC signed)

```json
{
  "v": 1,
  "agent_id": "uuid-of-sender",
  "name": "rack-12-server-04",
  "seq": 12345,
  "ts": 1726408200.123,
  "state": "OBSERVING",
  "health_summary": {
    "fan": "OK",
    "disk": "WARNING",
    "memory": "OK",
    "psu": "OK",
    "thermal": "OK"
  },
  "hmac": "a1b2c3d4e5f6...sha256-hex"
}
```

**HMAC Authentication (D18):**
- Algorithm: HMAC-SHA256
- Shared secret configured per site in `config.yaml` under `heartbeat.secret`
- HMAC computed over the JSON payload excluding the `hmac` field itself
- Packets with invalid or missing HMAC are rejected and logged as WARNING
- Prevents heartbeat spoofing on the management network

Max packet size: ~576 bytes (512 payload + 64 HMAC hex). No fragmentation.

### 9.3 Liveness Rules

| Condition | Threshold | Result |
|-----------|-----------|--------|
| Heartbeats received | < 30s between any two | `alive` |
| No heartbeat | 3 consecutive misses (30s) | `unresponsive` |
| First contact | First heartbeat ever received | `alive` (was `unknown`) |

### 9.4 Pre-Failure Evidence Retention

When a peer becomes unresponsive, the local agent retains the last 60 seconds of that peer's health summaries (from heartbeat packets). This is the "witness" evidence per R-AGENT crash resilience model.

Stored in the checkpoint database `peers` table with an additional `last_known_health_json` column containing the buffered summaries.

---

## 10. Site Manager Communication (R1 Stub)

### 10.1 R1 Scope

R1 ships a gRPC client stub. The agent can be configured with `--site-manager-ip` but operates fully standalone when no Site Manager is available (R-AGENT-3, R-MD7).

### 10.2 Stub Behavior

- If Site Manager is configured: agent attempts gRPC connection on startup, sends periodic heartbeats and verdict reports. Connection failures are logged and retried with exponential backoff (5s, 10s, 30s, 60s, max 300s).
- If Site Manager is not configured: agent runs in standalone mode. All verdicts are logged locally. Terminal UI is the primary output.
- gRPC proto definitions are deferred to pre-R2. R1 uses a minimal proto:

```protobuf
syntax = "proto3";

package harkeniq.v1;

service AgentService {
  rpc ReportVerdict(VerdictReport) returns (VerdictAck);
  rpc Heartbeat(AgentHeartbeat) returns (HeartbeatAck);
}

message VerdictReport {
  string agent_id = 1;
  string sensor_id = 2;
  string skill_name = 3;
  string verdict = 4;          // HEALTHY, WARNING, CRITICAL, TRENDING, UNKNOWN
  string evidence_json = 5;
  int64 timestamp_unix = 6;
}

message VerdictAck {
  bool accepted = 1;
}

message AgentHeartbeat {
  string agent_id = 1;
  string agent_name = 2;
  string state = 3;            // OBSERVING, EVALUATING, etc.
  map<string, string> health_summary = 4;
  int64 timestamp_unix = 5;
}

message HeartbeatAck {
  bool accepted = 1;
}
```

---

## 11. Agent State Machine (Implementation)

From Document 4, implemented as an explicit Python enum + transition table:

```
BOOTING → OBSERVING → EVALUATING → DECIDING → AWAITING_AUTH → ACTING → REPORTING → OBSERVING
                                       │                                     │
                                       └── no action needed ─────────────────┘
```

### 11.1 State Transitions

| From | To | Trigger |
|------|----|---------|
| BOOTING | OBSERVING | Config loaded, checkpoint restored, BMC connected |
| OBSERVING | EVALUATING | Sensor poll complete, skill conditions to check |
| EVALUATING | DECIDING | Verdict produced |
| DECIDING | OBSERVING | No action needed (verdict is informational) |
| DECIDING | AWAITING_AUTH | Action required, authorization needed |
| AWAITING_AUTH | ACTING | Authorized (locally allowed or SM approved) |
| AWAITING_AUTH | REPORTING | Authorization denied or timeout |
| ACTING | REPORTING | Action complete or failed |
| REPORTING | OBSERVING | Report sent (or queued if SM unavailable) |

### 11.2 Crash Recovery

On any unexpected termination:
1. systemd restarts the agent (via `Restart=on-failure`)
2. Agent enters BOOTING
3. Loads last checkpoint from SQLite
4. Any in-flight action (state was ACTING) is recorded as outcome UNKNOWN
5. Baselines and peer table are restored
6. Agent enters OBSERVING within 30 seconds (R-AGENT-3)

---

## 11A. Action Pipeline (D6, D16, D17)

R1 includes a full action pipeline with human approval gate. Actions are never autonomous -- they require operator approval via the TUI queue.

### 11A.1 R1 Action Allow-List

| Action Type | Redfish Operation | Risk | Reversible |
|-------------|------------------|------|------------|
| `IDENTIFY_LED` | `PATCH IndicatorLED = "Blinking"` | None | Yes (set to "Off") |
| `COLLECT_DIAGNOSTICS` | Dell: SCP export; HPE: AHS download | None (read-only) | N/A |
| `FAN_RESET` | `PATCH ThermalProfile = "Default"` | Low | Yes (restore previous) |

### 11A.2 Action Lifecycle

1. Skill evaluation produces a verdict with an `action` recommendation (Doc 07)
2. Agent creates an ActionRequest and enters AWAITING_AUTH
3. Action is added to the pending queue (displayed in TUI)
4. Operator reviews the queue and presses `a` (approve) or `d` (deny) per action
5. If approved: agent enters ACTING, executes the Redfish PATCH/POST
6. Agent verifies the result (e.g., `GET` to confirm LED state changed)
7. Agent enters REPORTING, logs outcome to audit trail
8. If denied: action logged as "denied", removed from queue, agent enters REPORTING

### 11A.3 Queue Behavior (D16)

- Queue-based: actions wait indefinitely until operator acts (no timeout auto-deny)
- Multiple actions can be pending simultaneously
- Oldest actions shown first
- Each action has a unique ID for CLI-based approval: `harken action approve <id>`
- Queue persisted in checkpoint.db (survives restart)

### 11A.4 Action Execution

Actions are executed via Redfish API calls. Each action type has a specific implementation:

```python
# IDENTIFY_LED
PATCH /redfish/v1/Chassis/{id}/Drives/{drive_id}
Body: {"IndicatorLED": "Blinking"}

# COLLECT_DIAGNOSTICS (Dell)
POST /redfish/v1/Dell/Managers/iDRAC.Embedded.1/DellLCService/Actions/DellLCService.ExportSystemConfiguration
Body: {"ExportFormat": "JSON", "ShareType": "Local"}

# COLLECT_DIAGNOSTICS (HPE)
GET /redfish/v1/Managers/1/ActiveHealthSystem (download AHS log)

# FAN_RESET
PATCH /redfish/v1/Managers/{id}/Oem/Dell/DellAttributes/iDRAC.Embedded.1
Body: {"Attributes": {"ThermalSettings.1.FanSpeedOffset": "Off"}}
```

---

## 11B. Terminal UI — Interactive Mode (D9)

### 11B.1 TUI Layout

```
┌─ HarkenIQ Agent ─────────────────────── rack-12-server-04 ─┐
│ Device: Dell PowerEdge R750 (iDRAC9)     Uptime: 2h 14m    │
│ State:  OBSERVING            Baseline: ESTABLISHED          │
├─────────────────────────────────────────────────────────────┤
│ SUBSYSTEM       STATUS    DETAIL                            │
│ Fan Health      ✓ OK      8/8 fans, 9200-10400 RPM         │
│ Disk Health     ⚠ WARN    Bay 2: SSD 18% life (TRENDING)   │
│ Memory Health   ✓ OK      16x 32GB DDR4, 0 ECC errors      │
│ PSU Health      ✓ OK      2x 1400W, redundant, 186W draw   │
│ Thermal         ✓ OK      Inlet 22°C / Exhaust 38°C        │
├─────────────────────────────────────────────────────────────┤
│ PEERS                                                       │
│ rack-12-srv-03  ✓ alive   2s ago                            │
│ rack-12-srv-05  ✓ alive   4s ago                            │
├─────────────────────────────────────────────────────────────┤
│ PENDING ACTIONS                                             │
│ [1] IDENTIFY_LED Disk.Bay.2 — SSD 18%      [a]pprove [d]eny│
│ [2] COLLECT_DIAGNOSTICS — PSU failure       [a]pprove [d]eny│
├─────────────────────────────────────────────────────────────┤
│ EVENTS (newest first)                                       │
│ 14:28:01  ⚠ Disk.Bay.2 SSD life dropped below 20%         │
│ 14:15:00  ✓ All subsystems healthy                         │
│ 12:01:33  ℹ Agent started, baseline restored               │
├─────────────────────────────────────────────────────────────┤
│ [f]ans [d]isks [m]emory [p]su [t]hermal [P]eers [h]elp    │
│ [a]pprove action [D]eny action  [q]uit                     │
└─────────────────────────────────────────────────────────────┘
```

### 11B.2 Hotkeys

| Key | Action |
|-----|--------|
| `f` | Drill into fan details (all fan sensors, RPM, thresholds) |
| `d` | Drill into disk details (all drives, SMART, SSD life) |
| `m` | Drill into memory details (all DIMMs, ECC counts) |
| `p` | Drill into PSU details (power draw, voltage, redundancy) |
| `t` | Drill into thermal details (all temp sensors, thresholds) |
| `P` | Drill into peer details (heartbeat history, pre-failure evidence) |
| `a` | Approve the highlighted pending action |
| `D` | Deny the highlighted pending action |
| `↑`/`↓` | Navigate pending actions list |
| `Esc` | Return to main dashboard from drill-down view |
| `h` | Show help overlay |
| `q` / `Ctrl+C` | Quit |

### 11B.3 Update Frequency

- Subsystem status: refreshed after each poll (60s)
- Peer status: refreshed after each heartbeat check (10s)
- Pending actions: refreshed on change (immediate)
- Events: appended on verdict change or peer status change
- Baseline indicator: "LEARNING" while confidence < 1.0, "ESTABLISHED" after

### 11B.4 Drill-Down Views

Each drill-down shows the full sensor data for that subsystem:

```
┌─ Fan Health Detail ──────────────────────────────────────┐
│                                                          │
│ NAME                  RPM     HEALTH  THRESHOLD  TREND   │
│ System Board Fan1A    9800    OK      480        -8/hr   │
│ System Board Fan1B    10200   OK      480        —       │
│ System Board Fan2A    9600    OK      480        —       │
│ System Board Fan2B    10400   OK      480        —       │
│ System Board Fan3A    9200    OK      480        —       │
│ System Board Fan3B    9800    OK      480        —       │
│ System Board Fan4A    10000   OK      480        —       │
│ System Board Fan4B    9600    OK      480        —       │
│                                                          │
│ Redundancy: OK (N+1, 4 min needed, 8 present)           │
│                                                          │
│ [Esc] Back to dashboard                                  │
└──────────────────────────────────────────────────────────┘
```

---

## 12. Packaging and Deployment

### 12.1 R1 Package Format

Python package installable via pip:

```bash
pip install harkeniq
# or from source:
pip install -e .
```

Directory structure:
```
harkeniq/
├── pyproject.toml
├── src/
│   └── harkeniq/
│       ├── __init__.py
│       ├── __main__.py           # CLI entry point
│       ├── cli.py                # Click-based CLI
│       ├── agent.py              # Main agent loop
│       ├── poller.py             # Redfish polling
│       ├── redfish/
│       │   ├── __init__.py
│       │   ├── client.py         # HTTP client (aiohttp)
│       │   ├── discovery.py      # BMC auto-detection
│       │   ├── normalize.py      # Vendor normalization
│       │   ├── dell.py           # Dell OEM handling
│       │   └── hpe.py            # HPE OEM handling
│       ├── skills/
│       │   ├── __init__.py
│       │   ├── engine.py         # Skill evaluation engine
│       │   ├── loader.py         # YAML skill loader
│       │   └── trending.py       # Baseline + trending logic
│       ├── heartbeat/
│       │   ├── __init__.py
│       │   ├── protocol.py       # UDP heartbeat send/recv
│       │   └── tracker.py        # Peer liveness tracking
│       ├── state/
│       │   ├── __init__.py
│       │   ├── checkpoint.py     # SQLite checkpoint manager
│       │   └── machine.py        # State machine implementation
│       ├── reporting/
│       │   ├── __init__.py
│       │   ├── grpc_stub.py      # Site Manager gRPC client (R1 stub)
│       │   └── console.py        # Terminal UI (rich)
│       ├── actions/
│       │   ├── __init__.py
│       │   ├── queue.py           # Action queue management
│       │   ├── executor.py        # Action execution (Redfish PATCH/POST)
│       │   └── types.py           # Action types and allow-list
│       ├── security/
│       │   ├── __init__.py
│       │   └── credentials.py    # AES-256-GCM credential encryption
│       └── mock/
│           ├── __init__.py
│           ├── simulator.py      # Redfish mock server
│           └── fixtures/         # Per-device JSON fixtures
│               ├── dell_r750/
│               ├── dell_r760/
│               ├── hpe_dl360_gen10/
│               └── hpe_dl380_gen11/
├── skills/                       # Default skill definitions
│   ├── fan-health.yaml
│   ├── disk-health.yaml
│   ├── memory-health.yaml
│   ├── psu-health.yaml
│   └── thermal-health.yaml
├── deploy/
│   ├── harkeniq.service          # systemd unit
│   ├── harkeniq.logrotate        # logrotate config
│   └── install.sh                # Installer script
└── tests/
    ├── test_poller.py
    ├── test_skills.py
    ├── test_normalize.py
    ├── test_heartbeat.py
    ├── test_checkpoint.py
    └── test_mock.py
```

### 12.2 Python Dependencies

```toml
[project]
requires-python = ">=3.11"
dependencies = [
    "aiohttp>=3.9",          # Async HTTP client for Redfish polling
    "click>=8.1",            # CLI framework
    "rich>=13.0",            # Terminal UI
    "pyyaml>=6.0",           # Skill definition parsing
    "grpcio>=1.60",          # gRPC client (Site Manager stub)
    "grpcio-tools>=1.60",    # Protobuf compilation
    "cryptography>=42.0",    # AES-256-GCM credential encryption
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "pytest-cov>=4.0",
    "aiohttp[speedups]",     # C extensions for production
]

[project.scripts]
harken = "harkeniq.cli:main"
```

### 12.3 systemd Service Unit

```ini
# /etc/systemd/system/harkeniq-agent.service

[Unit]
Description=HarkenIQ Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=exec
User=harkeniq
Group=harkeniq
ExecStart=/opt/harkeniq/bin/harken agent start
ExecReload=/bin/kill -HUP $MAINPID
Restart=on-failure
RestartSec=5
WatchdogSec=120

# Directories
StateDirectory=harkeniq
ConfigurationDirectory=harkeniq
LogsDirectory=harkeniq

# Security hardening
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/var/lib/harkeniq /var/log/harkeniq
PrivateTmp=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
MemoryDenyWriteExecute=yes

# Resource limits
MemoryMax=256M
CPUQuota=10%

[Install]
WantedBy=multi-user.target
```

### 12.4 Resource Ceilings (R-MD4)

| Resource | Limit | Enforcement |
|----------|-------|-------------|
| Memory | 256 MB max (systemd `MemoryMax`) | OOM-killed if exceeded |
| CPU | 10% of one core (systemd `CPUQuota`) | Throttled if exceeded |
| Disk writes | < 1 MB per checkpoint cycle | Application-level buffering |
| Network | < 100 KB/s to BMC (polling) | Application-level rate limiting |
| Open files | 64 (systemd `LimitNOFILE`) | Sufficient for SQLite + HTTP + UDP |

### 12.5 Installation Script

```bash
#!/bin/bash
# deploy/install.sh -- Install HarkenIQ agent on a server

set -euo pipefail

# Create service user
useradd --system --no-create-home --shell /usr/sbin/nologin harkeniq 2>/dev/null || true

# Install Python package
python3 -m venv /opt/harkeniq
/opt/harkeniq/bin/pip install harkeniq

# Symlink CLI
ln -sf /opt/harkeniq/bin/harken /usr/local/bin/harken

# Create directories
install -d -m 750 -o harkeniq -g harkeniq /var/lib/harkeniq
install -d -m 750 -o harkeniq -g harkeniq /var/log/harkeniq

# Install config template (if not present)
if [ ! -f /etc/harkeniq/config.yaml ]; then
    install -d -m 750 -o root -g harkeniq /etc/harkeniq
    harken config init > /etc/harkeniq/config.yaml
    chown root:harkeniq /etc/harkeniq/config.yaml
    chmod 640 /etc/harkeniq/config.yaml
fi

# Install default skills
install -d -m 750 -o root -g harkeniq /etc/harkeniq/skills
cp /opt/harkeniq/lib/python3.*/site-packages/harkeniq/skills/*.yaml /etc/harkeniq/skills/

# Install systemd unit
cp /opt/harkeniq/share/harkeniq.service /etc/systemd/system/harkeniq-agent.service
systemctl daemon-reload

# Install logrotate
cp /opt/harkeniq/share/harkeniq.logrotate /etc/logrotate.d/harkeniq

echo "HarkenIQ agent installed. Next steps:"
echo "  1. Configure BMC credentials: harken config init"
echo "  2. Test BMC connectivity:     harken bmc test"
echo "  3. Run a one-shot diagnosis:  harken diagnose"
echo "  4. Start the agent:           systemctl start harkeniq-agent"
echo "  5. Enable on boot:            systemctl enable harkeniq-agent"
```

---

## 13. Terminal UI (harken demo / --tui)

Built with Python `rich` library. Live-updating dashboard:

```
┌─ HarkenIQ Agent ─────────────────────────────── rack-12-server-04 ─┐
│ Device: Dell PowerEdge R750 (iDRAC9)     Uptime: 2h 14m           │
│ State:  OBSERVING                        Polls: 134                │
├────────────────────────────────────────────────────────────────────┤
│ SUBSYSTEM       STATUS    DETAIL                                   │
│ ─────────       ──────    ──────                                   │
│ Fan Health      ✓ OK      8/8 fans, 9200-10400 RPM                │
│ Disk Health     ⚠ WARN    Bay 2: SSD 18% life (TRENDING -2.1%/mo) │
│ Memory Health   ✓ OK      16x 32GB DDR4, 0 ECC errors             │
│ PSU Health      ✓ OK      2x 1400W, redundant, 186W draw          │
│ Thermal         ✓ OK      Inlet 22°C / Exhaust 38°C               │
├────────────────────────────────────────────────────────────────────┤
│ PEERS           STATUS    LAST SEEN                                │
│ ─────           ──────    ─────────                                │
│ rack-12-srv-03  ✓ alive   2s ago                                   │
│ rack-12-srv-05  ✓ alive   4s ago                                   │
├────────────────────────────────────────────────────────────────────┤
│ RECENT EVENTS                                                      │
│ 14:28:01  ⚠ Disk.Bay.2 SSD life dropped below 20%                │
│ 14:15:00  ✓ All subsystems healthy                                │
│ 12:01:33  ℹ Agent started, baseline restored from checkpoint      │
└────────────────────────────────────────────────────────────────────┘
```

---

## 14. Startup Sequence

```
1.  Parse CLI args and load config (config.yaml + env vars + CLI flags)
2.  Initialize logging
3.  Load or generate agent-id (/var/lib/harkeniq/agent-id)
4.  Load encrypted BMC credentials
5.  Auto-detect BMC (if bmc.host == "auto")
6.  Connect to BMC: GET /redfish/v1/ → identify vendor + controller generation
7.  Create Redfish session (X-Auth-Token)
8.  Restore checkpoint from SQLite (baselines, peer table, log cursors)
9.  Load skill definitions from /etc/harkeniq/skills/
10. Start heartbeat listener (UDP 5150)
11. Perform initial full inventory poll (storage, memory, fans, PSUs, thermal)
12. Enter OBSERVING state → begin main loop
13. [Optional] Connect to Site Manager (gRPC, non-blocking)
14. [Optional] Start terminal UI (if --tui or `harken demo`)

Target: steps 1-12 complete within 30 seconds (R-AGENT-3)
```

---

## 15. Graceful Shutdown

On SIGTERM (systemd stop) or SIGINT (Ctrl+C):

1. Stop accepting new poll cycles
2. Complete any in-progress Redfish request (3s timeout)
3. Send final heartbeat to peers with state = "SHUTTING_DOWN"
4. Force checkpoint (flush all in-memory state to SQLite)
5. Close Redfish session (DELETE session)
6. Close UDP socket
7. Close gRPC channel
8. Exit 0

Watchdog: systemd `WatchdogSec=120`. Agent sends `sd_notify(WATCHDOG=1)` every 60s. If the agent hangs, systemd kills and restarts it.
