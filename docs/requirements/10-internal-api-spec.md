# Document 10: Internal API Specification

**Purpose:** Implementation-ready specification of every domain object, module interface, exception, and data flow inside the HarkenIQ R1 agent. A developer should be able to write complete module stubs with correct signatures and types directly from this document.
**Scope:** All Python modules in `src/harkeniq/` as defined in Document 6 Section 12.1.
**Status:** Draft.
**Depends on:** Document 6 (Agent Runtime Architecture), Document 7 (Skill YAML Schema), Document 8 (Vendor Normalization), Document 13 (Baseline and Trending Algorithm).

---

## 1. Domain Object Definitions

All domain objects are Python `dataclasses` with explicit type annotations. All timestamps are ISO 8601 strings unless noted otherwise. All enums use Python `enum.Enum` with string values for JSON serialization.

### 1.1 Enums

```python
from enum import Enum

class VerdictSeverity(Enum):
    """Verdict severity levels with total ordering.

    Ordering (lowest to highest): UNKNOWN < HEALTHY < TRENDING < WARNING < CRITICAL.
    The ordering determines which verdict wins when multiple rules match the same
    sensor in a single evaluation cycle (highest severity wins).
    """
    UNKNOWN  = "UNKNOWN"
    HEALTHY  = "HEALTHY"
    TRENDING = "TRENDING"
    WARNING  = "WARNING"
    CRITICAL = "CRITICAL"

    def __lt__(self, other: "VerdictSeverity") -> bool:
        order = [
            VerdictSeverity.UNKNOWN,
            VerdictSeverity.HEALTHY,
            VerdictSeverity.TRENDING,
            VerdictSeverity.WARNING,
            VerdictSeverity.CRITICAL,
        ]
        return order.index(self) < order.index(other)

    def __le__(self, other: "VerdictSeverity") -> bool:
        return self == other or self < other

    def __gt__(self, other: "VerdictSeverity") -> bool:
        return not self <= other

    def __ge__(self, other: "VerdictSeverity") -> bool:
        return not self < other


class AgentState(Enum):
    """Agent state machine states (Doc 6, Section 11)."""
    BOOTING      = "BOOTING"
    OBSERVING    = "OBSERVING"
    EVALUATING   = "EVALUATING"
    DECIDING     = "DECIDING"
    AWAITING_AUTH = "AWAITING_AUTH"
    ACTING       = "ACTING"
    REPORTING    = "REPORTING"


class PeerStatus(Enum):
    """Peer liveness state (Doc 6, Section 9.3)."""
    UNKNOWN      = "UNKNOWN"
    ALIVE        = "ALIVE"
    UNRESPONSIVE = "UNRESPONSIVE"


class ActionType(Enum):
    """R1 action allow-list (Doc 7, Section 5.1)."""
    IDENTIFY_LED        = "IDENTIFY_LED"
    COLLECT_DIAGNOSTICS = "COLLECT_DIAGNOSTICS"
    FAN_RESET           = "FAN_RESET"


class ActionStatus(Enum):
    """Action lifecycle states (Doc 7, Section 5.3)."""
    PENDING   = "PENDING"
    APPROVED  = "APPROVED"
    DENIED    = "DENIED"
    EXECUTING = "EXECUTING"
    COMPLETED = "COMPLETED"
    FAILED    = "FAILED"
```

### 1.2 Sensor Reading (Raw)

```python
from dataclasses import dataclass, field
from typing import Any, Optional

@dataclass
class SensorReading:
    """Raw sensor reading from a single Redfish GET, before normalization.

    The poller produces one SensorReading per HTTP response. The raw_data dict
    is the deserialized JSON body. endpoint is the Redfish URI that was polled.
    """
    endpoint: str                     # Redfish URI, e.g. "/redfish/v1/Chassis/System.Embedded.1/Thermal"
    raw_data: dict[str, Any]          # Deserialized JSON response body
    sensor_type: str                  # "fan" | "disk" | "memory" | "psu" | "thermal" | "log" | "inventory"
    collected_at: str                 # ISO 8601 timestamp of poll completion
    http_status: int = 200            # HTTP status code from BMC
    response_time_ms: float = 0.0    # Round-trip time of the HTTP request
```

### 1.3 Normalized Data Models

The full normalized data model definitions live in `harkeniq.redfish.normalize` and are specified in Document 8, Sections 3.1--3.10. They are imported, not duplicated:

```python
from harkeniq.redfish.normalize import (
    NormalizedDevice,        # Top-level container: identity + all sensor collections + health rollup
    DeviceIdentity,          # Vendor, model, controller type/version, resource IDs
    NormalizedFan,           # Fan: name, speed_rpm, speed_pct, health, state, thresholds, redundancy
    NormalizedDisk,          # Disk: name, serial, media_type, protocol, health, life_left_pct, SMART
    NormalizedMemory,        # DIMM: name, capacity, type, speed, ECC metrics, alarm trips
    NormalizedPSU,           # PSU: name, capacity, output, input_voltage, health, redundancy
    NormalizedThermal,       # Temperature: name, reading_c, health, all threshold levels
    NormalizedPowerMetrics,  # System power: watts consumed, average, peak
    NormalizedLogEntry,      # Log entry: id, timestamp, severity, message, message_id, category
    HealthRollup,            # Per-subsystem health summary: fan, disk, memory, psu, thermal, overall
)
```

Each normalized class carries an `oem_data: dict` for vendor-specific fields. All fields have safe defaults for missing Redfish data (see Doc 8, Section 9).

### 1.4 Verdict

```python
@dataclass
class Verdict:
    """Result of evaluating one skill against one sensor for one poll cycle.

    The skill engine produces one Verdict per (sensor, skill) pair per evaluation.
    After debounce, the verdict may or may not update the sensor's persisted verdict state.
    """
    sensor_id: str                          # e.g. "fan:System Board Fan1A"
    skill_name: str                         # e.g. "fan-health"
    severity: VerdictSeverity               # Final severity after rule evaluation
    message: str                            # Human-readable message with field values substituted
    evidence: list["Evidence"]              # All evidence items that contributed to this verdict
    timestamp: str                          # ISO 8601 when verdict was produced
    debounce_state: "DebounceState"         # Current debounce window state for this verdict transition


@dataclass
class DebounceState:
    """Tracks N-of-M debounce for a single (sensor_id, skill_name) pair.

    The window is a fixed-size ring of recent raw verdicts (before debounce).
    The debounced verdict only changes when the N-of-M threshold is met.
    """
    sensor_id: str
    skill_name: str
    window: list[VerdictSeverity]           # Last M raw verdicts (most recent at end)
    window_size: int                        # M (from debounce config)
    threshold_count: int                    # N (number required to trigger transition)
    current_debounced: VerdictSeverity      # The currently active (debounced) verdict
    last_transition_at: Optional[str] = None  # ISO 8601 of last debounced verdict change
```

### 1.5 Evidence

```python
@dataclass
class Evidence:
    """Snapshot of the sensor data that caused a rule to match.

    Attached to every Verdict so that operators and the Site Manager can see
    exactly which fields triggered the verdict, without re-querying the BMC.
    """
    sensor_id: str                    # e.g. "fan:System Board Fan1A"
    skill_name: str                   # e.g. "fan-health"
    rule_index: int                   # 0-based position of the matching rule within the skill
    condition: str                    # The raw condition string from the skill YAML
    fields: dict[str, Any]            # Snapshot of all normalized fields for this sensor at evaluation time
    timestamp: str                    # ISO 8601 when the rule was evaluated
    baseline_confidence: float        # Baseline confidence at evaluation time (0.0--1.0)
```

### 1.6 Skill Definitions

```python
from typing import Union

# -- AST Node Types for parsed conditions (Doc 7, Section 9.2) --

ASTNode = Union["Comparison", "BooleanOp", "NotOp"]

@dataclass
class Comparison:
    """Leaf AST node: compares a field reference to a value."""
    field: str          # Normalized field name, e.g. "speed_rpm"
    operator: str       # One of: "==", "!=", "<", ">", "<=", ">="
    value: Any          # Literal number, string, boolean, or field reference string

@dataclass
class BooleanOp:
    """Internal AST node: combines two subtrees with AND or OR."""
    op: str             # "AND" or "OR"
    left: ASTNode
    right: ASTNode

@dataclass
class NotOp:
    """Internal AST node: negates a subtree."""
    operand: ASTNode


# -- Debounce Configuration --

@dataclass
class DebounceConfig:
    """Per-rule debounce override. If absent on a rule, global defaults apply."""
    count: int          # N: number of matching verdicts required
    window: int         # M: window of consecutive polls to examine


# -- Skill Rule --

@dataclass
class SkillRule:
    """A single detection rule within a skill definition.

    Rules are evaluated in order. All matching rules produce verdicts;
    the highest severity wins for the final sensor verdict.
    """
    condition: str                          # Raw condition string from YAML
    parsed_ast: ASTNode                     # Parsed AST from the expression parser
    verdict: VerdictSeverity                # Verdict to emit if condition matches
    message_template: str                   # Message with {field} placeholders
    debounce: Optional[DebounceConfig] = None  # Per-rule debounce override; None = use global
    action: Optional["ActionRecommendation"] = None  # Optional recommended action


@dataclass
class ActionRecommendation:
    """Action recommendation attached to a skill rule."""
    type: ActionType                        # From the R1 allow-list
    params: dict[str, str] = field(default_factory=dict)  # e.g. {"target": "{name}", "reason": "..."}


# -- Trending Rule --

@dataclass
class TrendingRule:
    """A trending detection rule within a skill definition."""
    field: str                              # Normalized field to track, e.g. "speed_rpm"
    direction: str                          # "declining" or "rising"
    verdict: VerdictSeverity                # Always VerdictSeverity.TRENDING
    message_template: str                   # Message with {field}, {rate}, {time_to_threshold} placeholders
    threshold_field: Optional[str] = None   # Field or literal to project toward, e.g. "threshold_low_critical"


# -- Skill Definition --

@dataclass
class SkillDefinition:
    """Complete parsed skill loaded from a YAML file.

    Loaded once at startup (and on SIGHUP reload). Immutable after loading.
    """
    name: str                               # Unique skill identifier, e.g. "fan-health"
    version: int                            # Schema version (must be 1 for R1)
    target: str                             # Sensor type: "fan" | "disk" | "memory" | "psu" | "thermal"
    description: str                        # Human-readable description
    rules: list[SkillRule]                  # Ordered detection rules (all evaluated, highest severity wins)
    trending: list[TrendingRule]            # Trending detection rules (evaluated independently)
    default_verdict: VerdictSeverity        # Applied when no rule matches (default: HEALTHY)
```

### 1.7 Peer and Heartbeat

```python
@dataclass
class Peer:
    """Tracked peer agent state, maintained by the heartbeat tracker."""
    peer_id: str                            # Peer agent UUID (from heartbeat packet)
    host: str                               # IP address or hostname
    port: int                               # UDP port
    last_heartbeat: Optional[str] = None    # ISO 8601 of last received heartbeat; None = never heard from
    status: PeerStatus = PeerStatus.UNKNOWN
    last_known_health: Optional[dict[str, str]] = None  # Last health_summary from heartbeat packet
    health_buffer: list[dict[str, str]] = field(default_factory=list)
    # ^ Last 60 seconds of health summaries (witness evidence per Doc 6, Section 9.4)
    name: Optional[str] = None              # Human-readable peer name from heartbeat


@dataclass
class HeartbeatPacket:
    """UDP heartbeat packet structure (Doc 6, Section 9.2).

    Serialized as JSON. Max 512 bytes. HMAC-SHA256 appended for authentication.
    """
    v: int                                  # Protocol version (1)
    agent_id: str                           # UUID of the sending agent
    name: str                               # Human-readable agent name
    seq: int                                # Monotonically increasing sequence number
    ts: float                               # Unix timestamp (time.time())
    state: str                              # AgentState value, e.g. "OBSERVING"
    health_summary: dict[str, str]          # Per-subsystem health: {"fan": "OK", "disk": "WARNING", ...}
    hmac: str = ""                          # HMAC-SHA256 hex digest (computed over all other fields)
```

### 1.8 State Machine

```python
@dataclass
class StateTransition:
    """Record of an agent state transition."""
    from_state: AgentState
    to_state: AgentState
    reason: str                             # Human-readable reason for the transition
    timestamp: str                          # ISO 8601 when the transition occurred
```

### 1.9 Actions

```python
@dataclass
class Action:
    """A proposed remediation action, flowing through the approval pipeline.

    Lifecycle: PENDING -> APPROVED -> EXECUTING -> COMPLETED/FAILED
                          DENIED (terminal)
    """
    id: str                                 # UUID for this action instance
    type: ActionType                        # From R1 allow-list
    params: dict[str, str]                  # Action-specific parameters (target, reason, etc.)
    status: ActionStatus = ActionStatus.PENDING
    sensor_id: str = ""                     # Sensor that triggered the action
    skill_name: str = ""                    # Skill that recommended the action
    verdict_severity: VerdictSeverity = VerdictSeverity.UNKNOWN
    proposed_at: str = ""                   # ISO 8601
    approved_at: Optional[str] = None       # ISO 8601; None if not yet approved
    completed_at: Optional[str] = None      # ISO 8601; None if not yet completed
    outcome: Optional["ActionOutcome"] = None  # Populated after execution


@dataclass
class ActionOutcome:
    """Result of executing an action."""
    action_id: str                          # References Action.id
    type: ActionType                        # Copy from Action.type
    target: str                             # Component that was acted on
    success: bool                           # True = completed successfully
    error_message: Optional[str] = None     # Populated on failure
    duration_ms: float = 0.0                # Execution time in milliseconds
    timestamp: str = ""                     # ISO 8601 when execution completed
```

### 1.10 Baseline and Trending

```python
import math

@dataclass
class Baseline:
    """Per-sensor baseline statistics, maintained incrementally (Doc 13).

    Uses Welford's online algorithm for mean/variance. Ring buffer stores
    recent (timestamp, value) samples. Regression state enables incremental
    OLS for trending.
    """
    sensor_id: str                          # e.g. "fan:System Board Fan1A"
    mean: float = 0.0                       # Running mean (Welford)
    stddev: float = 0.0                     # sqrt(variance)
    variance: float = 0.0                   # Population variance (Welford)
    m2: float = 0.0                         # Welford's M2 accumulator
    min_val: float = math.inf               # Minimum in ring buffer
    max_val: float = -math.inf              # Maximum in ring buffer
    sample_count: int = 0                   # Total samples ingested (may exceed buffer_size)
    ring_buffer: list[tuple[float, float]] = field(default_factory=list)
    # ^ Fixed-size FIFO of (unix_timestamp, value) tuples
    buffer_size: int = 1440                 # Configurable capacity (default 24h at 60s polling)
    first_sample_at: Optional[str] = None   # ISO 8601
    last_sample_at: Optional[str] = None    # ISO 8601
    degraded_baseline: bool = False         # True if learned during WARNING state
    critical_pause_remaining: int = 0       # Countdown: samples to skip after CRITICAL recovery

    # Confidence metric: min(1.0, sample_count / min_samples)
    # min_samples default = 60 (configurable)

    # Incremental regression state for trending (Doc 13, Section 3.2)
    regression_state: "RegressionState" = field(default_factory=lambda: RegressionState())

    @property
    def confidence(self) -> float:
        """Baseline confidence: 0.0 to 1.0."""
        min_samples = 60  # Default; overridden by config at runtime
        return min(1.0, self.sample_count / min_samples) if min_samples > 0 else 1.0


@dataclass
class RegressionState:
    """Incremental OLS linear regression state (Doc 13, Section 3.2).

    Maintained per-sensor alongside the baseline. Updated on each sample
    add/evict for O(1) per-sample cost.
    """
    sum_x: float = 0.0                      # Sum of x values (hours since first sample)
    sum_y: float = 0.0                      # Sum of y values (sensor readings)
    sum_xy: float = 0.0                     # Sum of x*y products
    sum_x2: float = 0.0                     # Sum of x-squared
    sum_y2: float = 0.0                     # Sum of y-squared
    n: int = 0                              # Sample count in regression
    eviction_count: int = 0                 # Counter for periodic full recomputation


@dataclass
class TrendResult:
    """Output of trending analysis for a single sensor field (Doc 13, Section 3.5)."""
    sensor_id: str                          # e.g. "fan:System Board Fan1A"
    field: str                              # e.g. "speed_rpm"
    slope: float                            # Units per hour (negative = declining)
    r_squared: float                        # Goodness of fit (0.0 to 1.0)
    direction: str                          # "rising" or "declining"
    current_value: float                    # Most recent sensor reading
    threshold_name: str                     # e.g. "threshold_low_critical"
    threshold_value: float                  # The threshold being approached
    time_to_threshold_hours: float          # Projected hours until threshold breach
    confidence: float                       # Baseline confidence (0.0 to 1.0)
    message: str                            # Human-readable summary
```

### 1.11 Log Entry

```python
@dataclass
class LogEntry:
    """Normalized hardware event log entry. Mirrors NormalizedLogEntry from Doc 8
    but adds a source field to distinguish log origin."""
    id: str = ""                            # Entry ID within the log service
    timestamp: str = ""                     # ISO 8601
    severity: str = ""                      # "OK" | "Warning" | "Critical"
    message: str = ""                       # Human-readable message
    message_id: str = ""                    # Structured message code (e.g. "SYS1003")
    component_id: Optional[str] = None      # Dell FQDD; None on HPE
    category: str = ""                      # Event category
    source: str = ""                        # "sel" (Dell) | "iml" (HPE)
```

---

## 2. Module Interface Contracts

Each module specifies its public async (or sync) function signatures, return types, exceptions, and dependencies. Private/internal functions are not listed.

### 2.1 harkeniq.agent

**Path:** `src/harkeniq/agent.py`
**Responsibility:** Main agent loop -- coordinates poller, heartbeat, reporting, and skill evaluation as concurrent asyncio tasks.

```python
from harkeniq.state.machine import StateMachine
from harkeniq.poller import Poller
from harkeniq.heartbeat.tracker import PeerTracker
from harkeniq.heartbeat.protocol import HeartbeatProtocol
from harkeniq.skills.engine import SkillEngine
from harkeniq.reporting.console import ConsoleReporter
from harkeniq.reporting.grpc_stub import GrpcReporter
from harkeniq.state.checkpoint import CheckpointManager

class Agent:
    """Top-level agent orchestrator.

    Owns the asyncio event loop and coordinates all concurrent tasks.
    Dependencies: all other modules (this is the composition root).
    """

    def __init__(self, config: dict) -> None:
        """Initialize agent with parsed configuration dict.

        Raises:
            ConfigError: If required configuration keys are missing or invalid.
        """
        ...

    async def start(self) -> None:
        """Main entry point. Runs the full startup sequence (Doc 6, Section 14)
        then enters the main loop.

        Steps:
          1. Load/generate agent-id
          2. Load encrypted BMC credentials
          3. Auto-detect or connect to BMC
          4. Create Redfish session
          5. Restore checkpoint from SQLite
          6. Load skill definitions
          7. Start heartbeat listener
          8. Perform initial full inventory poll
          9. Enter OBSERVING state and begin concurrent task groups
          10. Optionally connect to Site Manager
          11. Optionally start TUI

        Target: steps 1-9 complete within 30 seconds.

        Raises:
            ConfigError: On invalid configuration.
            RedfishConnectionError: If BMC is unreachable after auto-detect.
            RedfishAuthError: If BMC credentials are invalid.
        """
        ...

    async def stop(self) -> None:
        """Graceful shutdown (Doc 6, Section 15).

        1. Stop accepting new poll cycles.
        2. Complete in-progress Redfish request (3s timeout).
        3. Send final heartbeat with state=SHUTTING_DOWN.
        4. Force checkpoint.
        5. Close Redfish session.
        6. Close UDP socket.
        7. Close gRPC channel.
        """
        ...

    async def _run_poller_loop(self) -> None:
        """Sensor/log/inventory polling loop. Runs as asyncio task.

        On each sensor poll completion:
          1. Normalize raw data
          2. Update baselines
          3. Evaluate skills
          4. Apply debounce
          5. Emit verdicts
          6. Trigger checkpoint if interval elapsed

        Raises:
            RedfishError: Caught internally, logged, poll retried on next interval.
        """
        ...

    async def _run_heartbeat_loop(self) -> None:
        """Heartbeat send/receive loop. Runs as asyncio task.

        - Sends UDP heartbeat to all configured peers every heartbeat.interval seconds.
        - Checks peer liveness every heartbeat.interval * heartbeat.timeout_multiplier seconds.

        Raises:
            HeartbeatError: Caught internally, logged, next send retried.
        """
        ...

    async def _run_report_loop(self) -> None:
        """Site Manager reporting loop. Runs as asyncio task.

        - Sends verdict reports on verdict changes.
        - Sends periodic heartbeat to Site Manager every 60 seconds.
        - Operates in standalone mode (no-op) when site_manager.host is empty.

        Connection failures use exponential backoff: 5s, 10s, 30s, 60s, max 300s.
        """
        ...

    async def _on_signal(self, sig: int) -> None:
        """Handle SIGTERM/SIGINT by calling stop(). Handle SIGHUP by reloading skills."""
        ...
```

**Exceptions raised:** `ConfigError`, `RedfishConnectionError`, `RedfishAuthError` (on startup only; runtime errors are caught and logged).
**Dependencies:** All modules.

### 2.2 harkeniq.poller

**Path:** `src/harkeniq/poller.py`
**Responsibility:** Orchestrates Redfish polling across sensor types at configured intervals. Delegates HTTP to `redfish.client` and normalization to `redfish.normalize`.

```python
from harkeniq.redfish.client import RedfishClient
from harkeniq.redfish.normalize import NormalizedDevice, DeviceIdentity

class Poller:
    """Redfish polling orchestrator.

    Dependencies: redfish.client, redfish.normalize, redfish.dell, redfish.hpe.
    """

    def __init__(
        self,
        client: RedfishClient,
        identity: DeviceIdentity,
        config: dict,
    ) -> None:
        ...

    async def poll_sensors(self) -> NormalizedDevice:
        """Execute a full sensor poll cycle.

        Polls: Thermal (fans + temperatures), Power (PSUs + power metrics),
        Storage (disks), Memory (DIMMs + metrics), System (health rollup).

        Returns a NormalizedDevice with all sensor collections populated.
        collected_at is set to the current UTC time.

        Raises:
            RedfishConnectionError: If BMC is unreachable.
            RedfishTimeoutError: If any individual request exceeds timeout.
        """
        ...

    async def poll_logs(self, cursor: Optional[str] = None) -> tuple[list["NormalizedLogEntry"], str]:
        """Poll hardware event logs since the given cursor.

        Returns:
            Tuple of (new log entries, updated cursor string).

        Raises:
            RedfishConnectionError: If BMC is unreachable.
        """
        ...

    async def poll_inventory(self) -> NormalizedDevice:
        """Full inventory poll (storage, memory, fans, PSUs).

        Identical to poll_sensors but intended for the 300-second inventory cycle.
        May include additional deep enumeration (e.g., iterating all drive members).

        Raises:
            RedfishConnectionError: If BMC is unreachable.
        """
        ...
```

**Exceptions raised:** `RedfishConnectionError`, `RedfishTimeoutError`.
**Dependencies:** `redfish.client`, `redfish.normalize`, `redfish.dell`, `redfish.hpe`.

### 2.3 harkeniq.redfish.client

**Path:** `src/harkeniq/redfish/client.py`
**Responsibility:** Async HTTP client for BMC Redfish API. Manages sessions, TLS, retries, and rate limiting.

```python
import aiohttp
from typing import Any, Optional

class RedfishClient:
    """Async HTTP client for Redfish API communication with a single BMC.

    Uses aiohttp.ClientSession with connection pooling. Handles session token
    management (X-Auth-Token) and automatic renewal.

    Dependencies: aiohttp, harkeniq.security.credentials.
    """

    def __init__(
        self,
        host: str,
        port: int = 443,
        verify_ssl: bool = False,
        session_timeout: int = 300,
        request_timeout: int = 30,
    ) -> None:
        ...

    async def connect(self, username: str, password: str) -> None:
        """Establish a Redfish session with the BMC.

        Creates a session via POST /redfish/v1/SessionService/Sessions.
        Stores the X-Auth-Token for subsequent requests.

        Raises:
            RedfishConnectionError: If TCP/TLS connection fails.
            RedfishAuthError: If credentials are rejected (HTTP 401/403).
            RedfishTimeoutError: If connection attempt exceeds timeout.
        """
        ...

    async def get(self, path: str) -> dict[str, Any]:
        """Execute GET request against the BMC.

        Automatically handles:
          - X-Auth-Token header injection
          - Session renewal if token has expired (re-auth transparently)
          - HTTP 503 with Retry-After (wait and retry once)

        Args:
            path: Redfish URI path, e.g. "/redfish/v1/Chassis/System.Embedded.1/Thermal"

        Returns:
            Deserialized JSON response body as dict.

        Raises:
            RedfishConnectionError: If BMC is unreachable.
            RedfishAuthError: If re-authentication fails.
            RedfishTimeoutError: If request exceeds timeout.
            RedfishResponseError: If response is not valid JSON or HTTP status >= 400 (except 401/404/503).
        """
        ...

    async def patch(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """Execute PATCH request against the BMC (for actions like LED control).

        Args:
            path: Redfish URI path.
            body: JSON body to send.

        Returns:
            Deserialized JSON response body.

        Raises:
            RedfishConnectionError, RedfishAuthError, RedfishTimeoutError, RedfishResponseError.
        """
        ...

    async def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """Execute POST request against the BMC (for OEM actions).

        Raises:
            RedfishConnectionError, RedfishAuthError, RedfishTimeoutError, RedfishResponseError.
        """
        ...

    async def delete_session(self) -> None:
        """Close the Redfish session (DELETE the session resource).

        Called during graceful shutdown. Failure is logged but not raised.
        """
        ...

    async def close(self) -> None:
        """Close the underlying aiohttp session and release all connections."""
        ...
```

**Exceptions raised:** `RedfishConnectionError`, `RedfishAuthError`, `RedfishTimeoutError`, `RedfishResponseError`.
**Dependencies:** `aiohttp`, `harkeniq.security.credentials` (for credential retrieval).

### 2.4 harkeniq.redfish.discovery

**Path:** `src/harkeniq/redfish/discovery.py`
**Responsibility:** BMC auto-detection and vendor identification. Probes known USB-NIC addresses and identifies Dell/HPE.

```python
from harkeniq.redfish.client import RedfishClient
from harkeniq.redfish.normalize import DeviceIdentity

async def detect_bmc(
    configured_host: Optional[str] = None,
    port: int = 443,
    verify_ssl: bool = False,
    timeout: float = 3.0,
) -> str:
    """Auto-detect BMC IP address.

    If configured_host is provided and not "auto", returns it directly.
    Otherwise probes in order (Doc 6, Section 5.2):
      1. 169.254.0.1 (Dell USB-NIC)
      2. 169.254.0.2 (HPE USB-NIC)
      3. 127.0.0.1 (localhost fallback)

    Each probe: GET /redfish/v1/ with 3-second timeout. First 200 wins.

    Returns:
        BMC host IP address string.

    Raises:
        RedfishConnectionError: If no BMC is found at any probe address.
    """
    ...


async def identify_device(client: RedfishClient) -> DeviceIdentity:
    """Identify vendor, controller, and device metadata from BMC.

    Performs three Redfish GETs:
      1. GET /redfish/v1/ -> detect vendor (Dell/HPE from Oem namespace)
      2. GET /redfish/v1/Managers/{ManagerId} -> controller type and version
      3. GET /redfish/v1/Systems/{SystemId} -> model, serial, firmware

    Also probes for HPE SmartStorage on iLO5.

    Returns:
        Fully populated DeviceIdentity dataclass.

    Raises:
        RedfishConnectionError: If any required endpoint is unreachable.
        UnsupportedVendorError: If vendor cannot be identified.
    """
    ...
```

**Exceptions raised:** `RedfishConnectionError`, `UnsupportedVendorError`.
**Dependencies:** `redfish.client`, `redfish.normalize` (for `DeviceIdentity`, `detect_vendor`, `build_identity`).

### 2.5 harkeniq.redfish.normalize

**Path:** `src/harkeniq/redfish/normalize.py`
**Responsibility:** Vendor normalization dispatch -- routes raw Redfish JSON to Dell or HPE normalizers, returns normalized dataclasses. Contains all dataclass definitions (Doc 8, Section 3) and the public API (Doc 8, Section 10.1).

```python
# All function signatures are specified in Doc 8, Section 10.1.
# Reproduced here for completeness with full type annotations.

def detect_vendor(service_root: dict) -> str:
    """Detect vendor from /redfish/v1/ response. Returns 'Dell' or 'HPE'.

    Raises:
        UnsupportedVendorError: If neither Oem.Dell nor Oem.Hpe is found.
    """
    ...

def build_identity(
    service_root: dict,
    manager_data: dict,
    system_data: dict,
) -> DeviceIdentity:
    """Construct DeviceIdentity from startup Redfish responses.

    Raises:
        UnsupportedVendorError: If vendor cannot be determined.
    """
    ...

def normalize_fans(thermal_data: dict, identity: DeviceIdentity) -> list[NormalizedFan]:
    """Normalize Fans[] array from /Chassis/{id}/Thermal response.

    Returns empty list if Fans key is missing or thermal_data is malformed.
    Never raises. Logs WARNING on parse issues.
    """
    ...

def normalize_thermals(thermal_data: dict, identity: DeviceIdentity) -> list[NormalizedThermal]:
    """Normalize Temperatures[] array from /Chassis/{id}/Thermal response.

    Returns empty list if Temperatures key is missing.
    Never raises.
    """
    ...

def normalize_disks(
    storage_data: list[dict],
    identity: DeviceIdentity,
    smart_storage_data: list[dict] | None = None,
) -> list[NormalizedDisk]:
    """Normalize drive data from /Storage and optionally /SmartStorage.

    Deduplicates by serial number (standard path preferred over SmartStorage).
    Returns empty list if no drive data. Never raises.
    """
    ...

def normalize_memory(
    memory_data: list[dict],
    metrics_data: dict[str, dict],
    identity: DeviceIdentity,
) -> list[NormalizedMemory]:
    """Normalize DIMM data joined with MemoryMetrics by DIMM ID.

    Returns empty list if no memory data. Never raises.
    """
    ...

def normalize_psus(power_data: dict, identity: DeviceIdentity) -> list[NormalizedPSU]:
    """Normalize PowerSupplies[] from /Chassis/{id}/Power response.

    Copies first Redundancy entry's health/mode to every PSU.
    Returns empty list if PowerSupplies key is missing. Never raises.
    """
    ...

def normalize_power_metrics(power_data: dict, identity: DeviceIdentity) -> NormalizedPowerMetrics | None:
    """Normalize PowerControl[0] from /Chassis/{id}/Power response.

    Returns None if PowerControl is empty or missing. Never raises.
    """
    ...

def normalize_log_entries(entries_data: list[dict], identity: DeviceIdentity) -> list[NormalizedLogEntry]:
    """Normalize hardware event log entries. Never raises."""
    ...

def normalize_health_rollup(
    system_data: dict,
    identity: DeviceIdentity,
    fans: list[NormalizedFan] | None = None,
    disks: list[NormalizedDisk] | None = None,
    psus: list[NormalizedPSU] | None = None,
    thermals: list[NormalizedThermal] | None = None,
) -> HealthRollup:
    """Build health rollup from system-level data and normalized collections.

    Dell: derives subsystem health from normalized collections (worst-of).
    HPE: uses Oem.Hpe.AggregateHealthStatus when available.
    Never raises.
    """
    ...
```

**Exceptions raised:** `UnsupportedVendorError` (from `detect_vendor` and `build_identity` only). All normalize_* functions never raise -- they return empty collections on error.
**Dependencies:** `redfish.dell`, `redfish.hpe`.

### 2.6 harkeniq.redfish.dell

**Path:** `src/harkeniq/redfish/dell.py`
**Responsibility:** Dell-specific OEM field extraction and normalization helpers for iDRAC9/iDRAC10.

```python
from harkeniq.redfish.normalize import (
    DeviceIdentity, NormalizedFan, NormalizedDisk, NormalizedMemory,
    NormalizedPSU, NormalizedThermal, NormalizedLogEntry,
)

def extract_dell_fan_oem(fan_entry: dict) -> dict:
    """Extract Dell OEM fan fields (FanPWM, HardwareType) into oem_data dict.

    Returns dict with keys: fan_pwm, hardware_type. Missing fields omitted.
    """
    ...

def extract_dell_disk_oem(drive_entry: dict) -> dict:
    """Extract Dell OEM disk fields (RaidStatus, SmartAlertIndication, Slot, etc.).

    Returns dict with keys: raid_status, remaining_rated_write_endurance,
    smart_alert_indication, dell_slot.
    """
    ...

def resolve_dell_smart_alert(drive_entry: dict) -> bool:
    """Resolve SMART alert from both FailurePredicted and Dell OEM SmartAlertIndication.

    Returns True if either FailurePredicted == True or SmartAlertIndication == "Yes".
    """
    ...

def extract_dell_memory_oem(dimm_entry: dict) -> dict:
    """Extract Dell OEM memory fields (BankLabel, ManufactureDate).

    Returns dict with keys: bank_label, manufacture_date.
    """
    ...

def extract_dell_psu_oem(psu_entry: dict) -> dict:
    """Extract Dell OEM PSU fields (DetailedState, MaxInputPowerWatts).

    Returns dict with keys: detailed_state, max_input_power_watts.
    """
    ...

def extract_dell_log_oem(entry: dict) -> tuple[Optional[str], str, dict]:
    """Extract Dell OEM log fields.

    Returns:
        Tuple of (component_id/FQDD, category, oem_data dict).
        oem_data keys: fqdd, device_type, dell_category.
    """
    ...

def derive_dell_health_rollup(
    fans: list[NormalizedFan],
    disks: list[NormalizedDisk],
    psus: list[NormalizedPSU],
    thermals: list[NormalizedThermal],
) -> dict[str, str]:
    """Compute per-subsystem worst-case health for Dell (no OEM rollup endpoint in R1).

    Returns dict: {"fan": "OK", "disk": "Warning", ...}
    """
    ...
```

**Exceptions raised:** None. All functions return safe defaults on missing data.
**Dependencies:** None (pure data extraction).

### 2.7 harkeniq.redfish.hpe

**Path:** `src/harkeniq/redfish/hpe.py`
**Responsibility:** HPE-specific OEM field extraction and normalization helpers for iLO5/iLO6, including SmartStorage compatibility.

```python
from harkeniq.redfish.normalize import (
    DeviceIdentity, NormalizedFan, NormalizedDisk, NormalizedLogEntry,
)

def extract_hpe_fan_oem(fan_entry: dict) -> dict:
    """Extract HPE OEM fan fields (Location, HotPluggable).

    Returns dict with keys: location_detail, hot_pluggable.
    """
    ...

def resolve_hpe_fan_name(fan_entry: dict) -> str:
    """Resolve fan name: prefer FanName, fall back to Name (iLO5 firmware variation)."""
    ...

def extract_hpe_disk_oem(drive_entry: dict) -> dict:
    """Extract HPE OEM disk fields (CurrentTemperatureCelsius, PowerOnHours, etc.).

    Returns dict with keys: current_temperature_celsius, power_on_hours,
    drive_status, ssd_endurance_utilization_pct.
    """
    ...

def normalize_smart_storage_drive(ss_drive: dict) -> dict:
    """Transform a SmartStorage drive entry into standard-path-compatible dict.

    Applies transformations (Doc 8, Section 6.4):
      - InterfaceType -> Protocol
      - CapacityMiB * 1048576 -> CapacityBytes
      - 100 - SSDEnduranceUtilizationPercentage -> PredictedMediaLifeLeftPercent

    Returns a dict shaped like a standard /Storage drive entry.
    """
    ...

def extract_hpe_memory_oem(dimm_entry: dict) -> dict:
    """Extract HPE OEM memory fields (DIMMStatus).

    Returns dict with keys: dimm_status.
    """
    ...

def extract_hpe_psu_oem(psu_entry: dict) -> dict:
    """Extract HPE OEM PSU fields (BayNumber, HotPluggable, Mismatched, PowerSupplyStatus).

    Returns dict with keys: bay_number, hot_pluggable, mismatched, psu_status_state.
    """
    ...

def extract_hpe_log_oem(entry: dict) -> tuple[str, dict]:
    """Extract HPE OEM log fields.

    Returns:
        Tuple of (category string from Categories[0], oem_data dict).
        oem_data keys: class_code, event_code, categories, occurrence_count, repaired.
    """
    ...

def extract_hpe_aggregate_health(system_data: dict) -> dict[str, str]:
    """Extract per-subsystem health from Oem.Hpe.AggregateHealthStatus.

    Returns dict: {"fan": "OK", "disk": "Warning", ...}
    Returns all "Unknown" if AggregateHealthStatus is missing.
    """
    ...
```

**Exceptions raised:** None. All functions return safe defaults on missing data.
**Dependencies:** None (pure data extraction).

### 2.8 harkeniq.skills.engine

**Path:** `src/harkeniq/skills/engine.py`
**Responsibility:** Skill evaluation coordinator. Evaluates all loaded skills against normalized sensor data, applies debounce, and produces final verdicts.

```python
from harkeniq.skills.loader import SkillDefinition
from harkeniq.skills.expression import evaluate as evaluate_expression
from harkeniq.skills.trending import TrendingEngine

class SkillEngine:
    """Evaluates fault-detection skills against normalized sensor data.

    Dependencies: skills.expression, skills.loader, skills.trending.
    """

    def __init__(
        self,
        skills: list[SkillDefinition],
        debounce_config: dict,
    ) -> None:
        """Initialize with loaded skill definitions and global debounce settings.

        Args:
            skills: List of parsed SkillDefinition objects.
            debounce_config: Dict with keys "critical", "warning", "recovery",
                             each a [count, window] pair.
        """
        ...

    async def evaluate(
        self,
        device: "NormalizedDevice",
        baselines: dict[str, "Baseline"],
    ) -> list[Verdict]:
        """Evaluate all applicable skills against all sensors on the device.

        For each skill, iterates over every sensor matching skill.target.
        For each (skill, sensor) pair:
          1. Build context dict from normalized sensor fields + baseline fields
          2. Evaluate all rules (all matching rules produce evidence)
          3. Take highest severity verdict across matching rules
          4. Apply debounce to determine if verdict state actually transitions
          5. Produce final Verdict object

        Confidence gating (Doc 13, Section 2.3):
          - confidence < 0.5: skip expression evaluation, pass through BMC health only
          - confidence 0.5-0.99: expression evaluation enabled, trending disabled
          - confidence 1.0: full evaluation + trending

        Args:
            device: Normalized sensor data from the latest poll.
            baselines: Dict mapping sensor_id to Baseline objects.

        Returns:
            List of Verdict objects (one per sensor per skill).
            Only includes verdicts that passed debounce (i.e., effective state changes
            or confirmations of current state).
        """
        ...

    def _build_context(
        self,
        sensor: Any,
        baseline: Optional["Baseline"],
    ) -> dict[str, Any]:
        """Build evaluation context dict from a normalized sensor + baseline.

        Includes all normalized fields as top-level keys, plus:
          - baseline_mean: float (if confidence >= 0.5)
          - baseline_stddev: float (if confidence >= 0.5)
          - deviation: float (z-score, if confidence >= 0.5 and stddev > 0)

        Returns:
            Flat dict of field_name -> value for expression evaluation.
        """
        ...

    def _apply_debounce(
        self,
        sensor_id: str,
        skill_name: str,
        raw_severity: VerdictSeverity,
    ) -> tuple[VerdictSeverity, "DebounceState"]:
        """Apply N-of-M debounce to a raw verdict.

        Maintains internal debounce state per (sensor_id, skill_name) pair.

        Returns:
            Tuple of (debounced severity, current DebounceState).
        """
        ...

    def get_pending_actions(self) -> list[Action]:
        """Return all actions from the latest evaluation that need approval.

        Actions are collected during evaluate() from rules that matched and
        have an action recommendation.
        """
        ...

    def reload_skills(self, skills: list[SkillDefinition]) -> None:
        """Hot-reload skill definitions (called on SIGHUP).

        Preserves debounce state for skills that still exist.
        Clears debounce state for removed skills.
        """
        ...
```

**Exceptions raised:** `SkillEvaluationError` (only if AST evaluation encounters an internal error; should not happen with validated skills).
**Dependencies:** `skills.expression`, `skills.loader`, `skills.trending`, `redfish.normalize`.

### 2.9 harkeniq.skills.expression

**Path:** `src/harkeniq/skills/expression.py`
**Responsibility:** Expression DSL parser and evaluator. Parses condition strings into AST nodes, evaluates ASTs against sensor context dicts.

```python
from typing import Any

def parse(condition: str) -> ASTNode:
    """Parse a condition string into an AST.

    Grammar defined in Doc 7, Section 3.1.
    Maximum expression length: 1000 characters.
    Maximum AST depth: 20.

    Args:
        condition: Expression string, e.g. "health == 'Critical' AND state == 'Enabled'"

    Returns:
        Root ASTNode (Comparison, BooleanOp, or NotOp).

    Raises:
        SkillParseError: If the expression has syntax errors.
        SkillValidationError: If expression exceeds length or depth limits.
    """
    ...


def evaluate(node: ASTNode, context: dict[str, Any]) -> bool:
    """Evaluate an AST node against a sensor context dict.

    Type coercion rules (Doc 7, Section 3.6):
      - None on left side: always returns False (missing data never triggers)
      - Number vs number: numeric comparison
      - String vs string: case-sensitive comparison
      - Type mismatch: returns False with warning log

    If the right-side value is a string that matches a key in context,
    it is resolved as a field reference (e.g., threshold_low_critical
    resolves to the actual threshold value).

    Args:
        node: Parsed AST node.
        context: Dict mapping field names to sensor values.

    Returns:
        Boolean result of the condition.
    """
    ...


def validate_fields(node: ASTNode, target: str) -> list[str]:
    """Validate that all field references in the AST are valid for the given target.

    Args:
        node: Parsed AST.
        target: Sensor type ("fan", "disk", "memory", "psu", "thermal").

    Returns:
        List of unknown field names (empty if all valid).
    """
    ...


class Tokenizer:
    """Lexer for the expression DSL.

    Token types: IDENTIFIER, NUMBER, STRING, OPERATOR, KEYWORD, EOF.
    Keywords (case-insensitive): AND, OR, NOT.
    Operators: ==, !=, <, >, <=, >=.
    Strings: single-quoted.
    """

    def __init__(self, text: str) -> None: ...
    def next_token(self) -> "Token": ...
    def peek(self) -> "Token": ...


class Parser:
    """Recursive descent parser for the expression DSL.

    Produces AST nodes: Comparison, BooleanOp, NotOp.
    Precedence (highest to lowest): NOT, comparison operators, AND, OR.
    """

    def __init__(self, tokenizer: Tokenizer) -> None: ...
    def parse(self) -> ASTNode: ...
```

**Exceptions raised:** `SkillParseError`, `SkillValidationError`.
**Dependencies:** None (self-contained parser and evaluator).

### 2.10 harkeniq.skills.loader

**Path:** `src/harkeniq/skills/loader.py`
**Responsibility:** Load skill YAML files from disk, validate schema, parse conditions, and return `SkillDefinition` objects.

```python
from pathlib import Path

def load_skills(directory: Path) -> list[SkillDefinition]:
    """Load and validate all .yaml files from the skills directory.

    For each file:
      1. Parse YAML
      2. Validate required fields (name, version, target, rules)
      3. Validate version == 1
      4. Validate target in (fan, disk, memory, psu, thermal)
      5. Parse each rule condition through expression.parse()
      6. Validate field references against target type
      7. Validate verdict values
      8. Validate action types against R1 allow-list
      9. Validate debounce constraints (count <= window)
      10. Construct SkillDefinition with parsed ASTs

    Checks for duplicate skill names across all files.

    Args:
        directory: Path to the skills YAML directory.

    Returns:
        List of validated SkillDefinition objects.

    Raises:
        SkillValidationError: If any skill fails validation (with details).
        SkillParseError: If any condition expression has syntax errors.
    """
    ...


def load_skill_file(path: Path) -> SkillDefinition:
    """Load and validate a single skill YAML file.

    Raises:
        SkillValidationError: On schema validation failure.
        SkillParseError: On expression syntax error.
        FileNotFoundError: If path does not exist.
    """
    ...


def validate_skill(raw: dict, source_path: Optional[Path] = None) -> list[str]:
    """Validate a parsed YAML dict against the skill schema.

    Returns a list of validation error messages. Empty list = valid.
    Does not raise; caller decides whether to abort or continue.
    """
    ...
```

**Exceptions raised:** `SkillValidationError`, `SkillParseError`, `FileNotFoundError`.
**Dependencies:** `skills.expression` (for condition parsing), `pyyaml`.

### 2.11 harkeniq.skills.trending

**Path:** `src/harkeniq/skills/trending.py`
**Responsibility:** Baseline management and trend calculation. Maintains per-sensor baselines, updates Welford statistics, runs linear regression, and produces `TrendResult` verdicts.

```python
class TrendingEngine:
    """Manages per-sensor baselines and trending analysis.

    Dependencies: None (self-contained math).
    """

    def __init__(self, config: dict) -> None:
        """Initialize with trending/baseline configuration.

        Config keys used:
          - baseline.window_samples (default 1440)
          - baseline.min_samples (default 60)
          - baseline.critical_pause_samples (default 5)
          - trending.min_samples (default 60)
          - trending.slope_threshold (default 0.05)
          - trending.r_squared_min (default 0.5)
          - trending.max_projection_days (default 90)
        """
        ...

    def update_baseline(
        self,
        sensor_id: str,
        value: float,
        timestamp: float,
        current_health: str,
    ) -> Baseline:
        """Add a new sample to the sensor's baseline.

        Applies Welford update/evict, ring buffer management, and regression
        state update. Respects CRITICAL state freeze and recovery pause.

        Handles edge cases:
          - Sudden discontinuity (> 5 sigma): reset baseline entirely.
          - Counter-type sensors: caller must pass delta, not raw count.
          - Time gap > 5 * expected_interval: insert gap marker.

        Args:
            sensor_id: Unique sensor identifier.
            value: Current sensor reading (numeric).
            timestamp: Unix timestamp of the reading.
            current_health: BMC health status ("OK", "Warning", "Critical").

        Returns:
            Updated Baseline object.
        """
        ...

    def compute_trend(
        self,
        sensor_id: str,
        trending_rules: list["TrendingRule"],
        context: dict[str, Any],
    ) -> list[TrendResult]:
        """Compute trending verdicts for a sensor.

        Only runs when baseline confidence == 1.0.

        For each trending rule:
          1. Check ring buffer has >= trending.min_samples
          2. Compute slope and R-squared from regression state
          3. Check slope significance (> slope_threshold)
          4. Check goodness of fit (R-squared > r_squared_min)
          5. Check direction matches rule
          6. Compute time-to-threshold projection
          7. Guard: 0 < time_to_threshold < max_projection_days * 24

        Args:
            sensor_id: Sensor identifier.
            trending_rules: Trending rules from the skill definition.
            context: Current sensor context dict (for threshold value resolution).

        Returns:
            List of TrendResult objects (may be empty).
        """
        ...

    def get_baseline(self, sensor_id: str) -> Optional[Baseline]:
        """Retrieve the current baseline for a sensor. Returns None if not yet tracked."""
        ...

    def get_all_baselines(self) -> dict[str, Baseline]:
        """Return all baselines (for checkpoint persistence)."""
        ...

    def restore_baselines(self, baselines: dict[str, Baseline]) -> None:
        """Restore baselines from checkpoint data (called on startup)."""
        ...

    def _welford_update(self, baseline: Baseline, value: float) -> None:
        """Apply Welford's online algorithm to add a new sample."""
        ...

    def _welford_remove(self, baseline: Baseline, old_value: float) -> None:
        """Apply inverse Welford to remove an evicted sample."""
        ...

    def _regression_add(self, reg: RegressionState, x: float, y: float) -> None:
        """Add a sample to the incremental regression state."""
        ...

    def _regression_remove(self, reg: RegressionState, x: float, y: float) -> None:
        """Remove a sample from the incremental regression state."""
        ...

    def _compute_regression(self, reg: RegressionState) -> tuple[float, float, float]:
        """Compute slope, intercept, and R-squared from regression state.

        Returns:
            Tuple of (slope, intercept, r_squared). Returns (0.0, 0.0, 0.0)
            if n < 2 or denominator is zero.
        """
        ...
```

**Exceptions raised:** None. All methods are safe and handle edge cases internally.
**Dependencies:** `math` (stdlib only).

### 2.12 harkeniq.heartbeat.protocol

**Path:** `src/harkeniq/heartbeat/protocol.py`
**Responsibility:** UDP heartbeat packet send/receive with HMAC-SHA256 authentication.

```python
import asyncio

class HeartbeatProtocol(asyncio.DatagramProtocol):
    """UDP heartbeat send/receive with HMAC-SHA256 authentication.

    Implements asyncio.DatagramProtocol for integration with the event loop.

    Dependencies: hmac, hashlib, json (stdlib).
    """

    def __init__(
        self,
        agent_id: str,
        agent_name: str,
        shared_secret: bytes,
        on_receive: "Callable[[HeartbeatPacket, tuple[str, int]], Awaitable[None]]",
    ) -> None:
        """Initialize the heartbeat protocol handler.

        Args:
            agent_id: This agent's UUID.
            agent_name: This agent's human-readable name.
            shared_secret: HMAC shared secret (from site config).
            on_receive: Async callback invoked when a valid heartbeat is received.
        """
        ...

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        """Called when the UDP socket is opened."""
        ...

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        """Process incoming UDP datagram.

        1. Deserialize JSON
        2. Extract and verify HMAC
        3. Validate packet structure (v, agent_id, seq, ts, state, health_summary)
        4. Call on_receive callback with parsed HeartbeatPacket

        Invalid or tampered packets are logged and dropped silently.
        """
        ...

    async def send_heartbeat(
        self,
        target_host: str,
        target_port: int,
        state: AgentState,
        health_summary: dict[str, str],
        seq: int,
    ) -> None:
        """Construct and send a heartbeat packet to a peer.

        Builds HeartbeatPacket, serializes to JSON, computes HMAC,
        appends HMAC to packet, sends via UDP transport.

        Max packet size: 512 bytes.

        Raises:
            HeartbeatSendError: If the UDP send fails (socket error).
        """
        ...

    def _compute_hmac(self, payload: bytes) -> str:
        """Compute HMAC-SHA256 hex digest over the packet payload.

        Uses self.shared_secret as the HMAC key.
        """
        ...

    def _verify_hmac(self, payload: bytes, expected_hmac: str) -> bool:
        """Verify HMAC using constant-time comparison."""
        ...

    def connection_lost(self, exc: Optional[Exception]) -> None:
        """Called when the UDP socket is closed."""
        ...
```

**Exceptions raised:** `HeartbeatSendError`.
**Dependencies:** `hmac`, `hashlib`, `json`, `asyncio` (all stdlib).

### 2.13 harkeniq.heartbeat.tracker

**Path:** `src/harkeniq/heartbeat/tracker.py`
**Responsibility:** Peer liveness tracking. Evaluates heartbeat timing to determine peer status (ALIVE, UNRESPONSIVE, UNKNOWN).

```python
class PeerTracker:
    """Tracks peer liveness based on heartbeat timing.

    Dependencies: heartbeat.protocol (for HeartbeatPacket).
    """

    def __init__(
        self,
        configured_peers: list[dict[str, Any]],
        heartbeat_interval: int = 10,
        timeout_multiplier: int = 3,
    ) -> None:
        """Initialize with configured peer list.

        Creates Peer objects for each configured peer with status UNKNOWN.

        Args:
            configured_peers: List of {"host": str, "port": int} dicts.
            heartbeat_interval: Seconds between heartbeats (default 10).
            timeout_multiplier: Missed heartbeats before UNRESPONSIVE (default 3).
        """
        ...

    async def on_heartbeat_received(
        self,
        packet: HeartbeatPacket,
        addr: tuple[str, int],
    ) -> None:
        """Process a received heartbeat from a peer.

        Updates the peer's:
          - last_heartbeat timestamp
          - status to ALIVE
          - last_known_health
          - health_buffer (appends, keeps last 60 seconds)
          - name (from packet)
          - peer_id (from packet.agent_id)

        If this is the first heartbeat from this peer, transitions from UNKNOWN to ALIVE.
        """
        ...

    def check_liveness(self) -> list[tuple[Peer, PeerStatus, PeerStatus]]:
        """Evaluate all peers and detect status transitions.

        A peer is UNRESPONSIVE if no heartbeat received in
        (heartbeat_interval * timeout_multiplier) seconds.

        Returns:
            List of (peer, old_status, new_status) tuples for peers whose
            status changed. Empty list if no changes.
        """
        ...

    def get_peers(self) -> list[Peer]:
        """Return all tracked peers."""
        ...

    def get_peer_summary(self) -> dict[str, str]:
        """Return peer health summary for heartbeat packets.

        Returns dict mapping peer_id to status string.
        """
        ...

    def restore_peers(self, peers: list[Peer]) -> None:
        """Restore peer state from checkpoint (called on startup)."""
        ...
```

**Exceptions raised:** None.
**Dependencies:** `heartbeat.protocol` (for `HeartbeatPacket` type).

### 2.14 harkeniq.state.checkpoint

**Path:** `src/harkeniq/state/checkpoint.py`
**Responsibility:** SQLite-based state persistence with WAL mode. Reads and writes all persistent state tables (Doc 6, Sections 7.1--7.3).

```python
from pathlib import Path

class CheckpointManager:
    """SQLite checkpoint manager for agent state persistence.

    Uses WAL mode for non-blocking reads during writes.
    Schema defined in Doc 6, Section 7.2 and Doc 13, Section 7.1.

    Dependencies: sqlite3 (stdlib).
    """

    def __init__(self, db_path: Path) -> None:
        """Open or create the checkpoint database.

        Creates all tables if they do not exist.
        Enables WAL mode.

        Raises:
            CheckpointWriteError: If the database cannot be opened or initialized.
        """
        ...

    async def save_checkpoint(
        self,
        sensor_readings: dict[str, dict],
        baselines: dict[str, Baseline],
        verdicts: list[Verdict],
        peers: list[Peer],
        agent_meta: dict[str, str],
        log_cursors: dict[str, str],
    ) -> None:
        """Persist all agent state to SQLite in a single transaction.

        Only writes dirty baselines (those changed since last checkpoint).
        Prunes verdict history to last 1000 per sensor.

        Raises:
            CheckpointWriteError: If the write transaction fails.
        """
        ...

    async def load_checkpoint(self) -> dict[str, Any]:
        """Load all persisted state from SQLite.

        Returns a dict with keys:
          - "agent_meta": dict[str, str]
          - "sensor_readings": dict[str, dict]
          - "baselines": dict[str, Baseline]
          - "verdicts": list[Verdict] (most recent per sensor)
          - "peers": list[Peer]
          - "log_cursors": dict[str, str]

        Discards baselines older than max_baseline_age_days.

        Raises:
            CheckpointReadError: If the database is corrupt or unreadable.
        """
        ...

    async def save_audit_entry(
        self,
        action: str,
        target: str,
        outcome: str,
        authorization: Optional[str] = None,
        evidence_json: Optional[str] = None,
    ) -> None:
        """Append an entry to the audit_log table (append-only, never pruned).

        Raises:
            CheckpointWriteError: If the write fails.
        """
        ...

    async def update_log_cursor(self, log_source: str, last_entry_id: str) -> None:
        """Update the log cursor for incremental log polling.

        Raises:
            CheckpointWriteError: If the write fails.
        """
        ...

    async def get_log_cursor(self, log_source: str) -> Optional[str]:
        """Retrieve the last processed log entry ID for a given source.

        Returns None if no cursor exists for this source.

        Raises:
            CheckpointReadError: If the read fails.
        """
        ...

    async def close(self) -> None:
        """Close the database connection."""
        ...
```

**Exceptions raised:** `CheckpointWriteError`, `CheckpointReadError`.
**Dependencies:** `sqlite3` (stdlib).

### 2.15 harkeniq.state.machine

**Path:** `src/harkeniq/state/machine.py`
**Responsibility:** Explicit state machine with defined transitions and transition validation (Doc 6, Section 11).

```python
class StateMachine:
    """Agent state machine with validated transitions.

    Transition table (Doc 6, Section 11.1):
      BOOTING -> OBSERVING
      OBSERVING -> EVALUATING
      EVALUATING -> DECIDING
      DECIDING -> OBSERVING (no action needed)
      DECIDING -> AWAITING_AUTH (action required)
      AWAITING_AUTH -> ACTING (authorized)
      AWAITING_AUTH -> REPORTING (denied or timeout)
      ACTING -> REPORTING
      REPORTING -> OBSERVING

    Dependencies: None.
    """

    TRANSITIONS: dict[AgentState, set[AgentState]] = {
        AgentState.BOOTING:       {AgentState.OBSERVING},
        AgentState.OBSERVING:     {AgentState.EVALUATING},
        AgentState.EVALUATING:    {AgentState.DECIDING},
        AgentState.DECIDING:      {AgentState.OBSERVING, AgentState.AWAITING_AUTH},
        AgentState.AWAITING_AUTH: {AgentState.ACTING, AgentState.REPORTING},
        AgentState.ACTING:        {AgentState.REPORTING},
        AgentState.REPORTING:     {AgentState.OBSERVING},
    }

    def __init__(self) -> None:
        """Initialize in BOOTING state."""
        ...

    @property
    def current_state(self) -> AgentState:
        """Return the current agent state."""
        ...

    def transition(self, to_state: AgentState, reason: str) -> StateTransition:
        """Transition to a new state.

        Validates that the transition is allowed by the transition table.
        Records the transition in history.

        Args:
            to_state: Target state.
            reason: Human-readable reason for the transition.

        Returns:
            StateTransition record.

        Raises:
            ValueError: If the transition is not allowed (from_state -> to_state not in table).
        """
        ...

    def get_history(self) -> list[StateTransition]:
        """Return the full transition history (most recent last)."""
        ...

    def can_transition(self, to_state: AgentState) -> bool:
        """Check if a transition to the given state is allowed from the current state."""
        ...
```

**Exceptions raised:** `ValueError` on invalid transition.
**Dependencies:** None.

### 2.16 harkeniq.reporting.console

**Path:** `src/harkeniq/reporting/console.py`
**Responsibility:** Terminal UI dashboard using the `rich` library. Renders live-updating sensor health, peer status, events, and action approval queue.

```python
from rich.live import Live

class ConsoleReporter:
    """Terminal UI dashboard (Doc 6, Section 13).

    Dependencies: rich library.
    """

    def __init__(self, agent_name: str, device_model: str) -> None:
        """Initialize the console reporter.

        Args:
            agent_name: Human-readable agent name for the header.
            device_model: Device model string (e.g., "Dell PowerEdge R750 (iDRAC9)").
        """
        ...

    async def start(self) -> None:
        """Start the live-updating TUI. Begins a rich.Live context."""
        ...

    async def stop(self) -> None:
        """Stop the TUI and restore the terminal."""
        ...

    def update_verdicts(self, verdicts: list[Verdict]) -> None:
        """Update the subsystem health table with latest verdicts."""
        ...

    def update_peers(self, peers: list[Peer]) -> None:
        """Update the peer status table."""
        ...

    def update_trending(self, trends: list[TrendResult]) -> None:
        """Update the trending display section."""
        ...

    def add_event(self, message: str, severity: VerdictSeverity) -> None:
        """Add an event to the recent events feed (scrolling list, last 20)."""
        ...

    def update_agent_state(self, state: AgentState, poll_count: int, uptime_seconds: float) -> None:
        """Update the agent state and metadata in the header."""
        ...

    def update_learning_status(self, sensor_id: str, sample_count: int, min_samples: int) -> None:
        """Update the LEARNING indicator for a sensor during baseline warmup."""
        ...

    def show_action_queue(self, actions: list[Action]) -> None:
        """Display pending actions awaiting operator approval."""
        ...

    def prompt_action_approval(self, action: Action) -> bool:
        """Show action details and prompt operator for approval.

        Blocks until operator responds. Used in interactive TUI mode.

        Returns:
            True if approved, False if denied.
        """
        ...
```

**Exceptions raised:** None (display-only).
**Dependencies:** `rich`.

### 2.17 harkeniq.reporting.grpc_stub

**Path:** `src/harkeniq/reporting/grpc_stub.py`
**Responsibility:** Site Manager gRPC client (R1 stub). Reports verdicts and heartbeats. Operates as no-op in standalone mode.

```python
class GrpcReporter:
    """gRPC client for Site Manager communication (R1 stub).

    Proto defined in Doc 6, Section 10.2.
    Standalone mode: all methods are no-ops when host is empty.
    Connection failures: logged and retried with exponential backoff
    (5s, 10s, 30s, 60s, max 300s).

    Dependencies: grpcio.
    """

    def __init__(self, host: str, port: int = 50051) -> None:
        """Initialize gRPC client.

        If host is empty, operates in standalone mode (all calls are no-ops).
        """
        ...

    async def connect(self) -> None:
        """Establish gRPC channel to Site Manager.

        No-op in standalone mode.

        Raises:
            GrpcConnectionError: If connection fails (retried with backoff).
        """
        ...

    async def report_verdict(self, verdict: Verdict) -> bool:
        """Send a verdict report to the Site Manager.

        Returns True if accepted, False if rejected or connection failed.
        Connection failures are logged; does not raise.
        """
        ...

    async def send_heartbeat(
        self,
        agent_id: str,
        agent_name: str,
        state: AgentState,
        health_summary: dict[str, str],
    ) -> bool:
        """Send agent heartbeat to Site Manager.

        Returns True if accepted, False otherwise. Does not raise.
        """
        ...

    async def close(self) -> None:
        """Close the gRPC channel."""
        ...

    @property
    def is_standalone(self) -> bool:
        """True if no Site Manager is configured."""
        ...

    @property
    def is_connected(self) -> bool:
        """True if gRPC channel is open and healthy."""
        ...
```

**Exceptions raised:** None externally. `GrpcConnectionError` is caught internally with backoff retry.
**Dependencies:** `grpcio`.

### 2.18 harkeniq.security.credentials

**Path:** `src/harkeniq/security/credentials.py`
**Responsibility:** BMC credential encryption/decryption using AES-256-GCM (Doc 6, Section 3.3).

```python
from pathlib import Path

def load_credentials(
    credentials_path: Path,
    secret_path: Path,
    machine_id_path: Path = Path("/etc/machine-id"),
) -> dict[str, str]:
    """Load and decrypt BMC credentials.

    Decryption:
      1. Read agent-secret from secret_path
      2. Read machine-id
      3. Derive key: PBKDF2(machine-id || agent-secret, salt, 100000 iterations)
      4. Decrypt AES-256-GCM ciphertext from credentials_path
      5. Parse YAML plaintext

    Returns:
        Dict with keys "username" and "password".

    Raises:
        ConfigError: If credentials file or secret file is missing.
        ConfigError: If decryption fails (wrong key, corrupt file).
    """
    ...


def save_credentials(
    credentials: dict[str, str],
    credentials_path: Path,
    secret_path: Path,
    machine_id_path: Path = Path("/etc/machine-id"),
) -> None:
    """Encrypt and save BMC credentials.

    1. Generate random salt (16 bytes) and nonce (12 bytes)
    2. Derive key: PBKDF2(machine-id || agent-secret, salt, 100000 iterations)
    3. Serialize credentials as YAML
    4. Encrypt with AES-256-GCM
    5. Write salt + nonce + ciphertext + tag to credentials_path

    Raises:
        ConfigError: If secret file is missing or paths are not writable.
    """
    ...


def generate_agent_secret(secret_path: Path) -> bytes:
    """Generate and persist a new agent secret (32 random bytes).

    Creates the file with mode 0600. Only called on first run.

    Returns:
        The generated secret bytes.

    Raises:
        ConfigError: If the path is not writable.
    """
    ...


def derive_key(
    machine_id: bytes,
    agent_secret: bytes,
    salt: bytes,
    iterations: int = 100_000,
) -> bytes:
    """Derive AES-256 key using PBKDF2-HMAC-SHA256.

    Key material: machine_id || agent_secret.
    Output: 32-byte key.
    """
    ...
```

**Exceptions raised:** `ConfigError`.
**Dependencies:** `cryptography` (for AES-256-GCM and PBKDF2).

### 2.19 harkeniq.mock.simulator

**Path:** `src/harkeniq/mock/simulator.py`
**Responsibility:** Redfish mock server for testing and demo. Serves fixture JSON from disk with support for dynamic fault injection.

```python
from aiohttp import web
from pathlib import Path

class RedfishMockSimulator:
    """Mock Redfish BMC server for development, testing, and harken demo.

    Serves static JSON fixtures from the fixtures directory.
    Supports dynamic fault injection for demo scenarios.

    Fixtures directory structure:
      fixtures/dell_r750/
      fixtures/dell_r760/
      fixtures/hpe_dl360_gen10/
      fixtures/hpe_dl380_gen11/

    Dependencies: aiohttp (server mode).
    """

    def __init__(
        self,
        fixture_dir: Path,
        host: str = "127.0.0.1",
        port: int = 8443,
    ) -> None:
        """Initialize the mock simulator.

        Args:
            fixture_dir: Path to the fixture directory (e.g., fixtures/dell_r750/).
            host: Bind address.
            port: Bind port.
        """
        ...

    async def start(self) -> None:
        """Start the mock HTTP server.

        Registers routes for all standard Redfish endpoints.
        """
        ...

    async def stop(self) -> None:
        """Stop the mock server."""
        ...

    def inject_fault(self, fault_type: str, params: dict) -> None:
        """Inject a fault into the mock responses.

        Supported fault types for demo (Doc 9):
          - "fan_failure": Set a fan to Critical health, 0 RPM
          - "disk_smart": Set a disk to SMART predictive failure
          - "memory_ecc": Increase ECC error counts
          - "psu_redundancy": Remove a PSU (set Absent)
          - "thermal_warning": Raise temperature above warning threshold

        Args:
            fault_type: One of the supported fault type strings.
            params: Fault-specific parameters (e.g., which fan, which disk).
        """
        ...

    def clear_faults(self) -> None:
        """Remove all injected faults, restore baseline fixture responses."""
        ...

    def set_scenario(self, scenario: str, speed: float = 1.0) -> None:
        """Configure a timed fault scenario for harken demo.

        Scenarios inject faults at specific time offsets to demonstrate
        the agent's detection and trending capabilities.

        Args:
            scenario: Scenario name ("all", "fan-failure", "disk-smart", etc.).
            speed: Time compression factor (1.0 = real-time, 10.0 = 10x speed).
        """
        ...
```

**Exceptions raised:** None externally.
**Dependencies:** `aiohttp` (web server).

### 2.20 harkeniq.cli

**Path:** `src/harkeniq/cli.py`
**Responsibility:** Click-based CLI command definitions (Doc 6, Section 6).

```python
import click

@click.group()
def main() -> None:
    """HarkenIQ -- Autonomous hardware operations agent."""
    ...

# -- agent subgroup --

@main.group()
def agent() -> None:
    """Agent lifecycle management."""
    ...

@agent.command()
@click.option("--bmc-ip", type=str, default=None, help="BMC IP address")
@click.option("--bmc-user", type=str, default=None, help="BMC username")
@click.option("--bmc-pass", type=str, default=None, help="BMC password")
@click.option("--site-manager-ip", type=str, default=None, help="Site Manager address")
@click.option("--peers", type=str, default=None, help="Comma-separated peer list")
@click.option("--foreground", is_flag=True, default=False, help="Run in foreground")
@click.option("--tui", is_flag=True, default=False, help="Enable terminal UI dashboard")
@click.option("--log-level", type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"]), default=None)
def start(**kwargs) -> None:
    """Start the HarkenIQ agent."""
    ...

@agent.command()
def stop() -> None:
    """Stop the HarkenIQ agent (sends SIGTERM)."""
    ...

@agent.command()
def status() -> None:
    """Show agent state, uptime, peer table, last verdict."""
    ...

@agent.command()
def checkpoint() -> None:
    """Force an immediate state checkpoint."""
    ...

# -- diagnose command --

@main.command()
@click.option("--bmc-ip", type=str, default=None)
@click.option("--json", "output_json", is_flag=True, default=False)
@click.option("--verbose", is_flag=True, default=False)
def diagnose(**kwargs) -> None:
    """One-shot: poll BMC, evaluate skills, print results, exit.

    Exit codes (Nagios convention):
      0 = all healthy
      1 = warning-level verdict
      2 = critical-level verdict
      3 = unknown (BMC unreachable or skill error)
      4 = configuration error
    """
    ...

# -- demo command --

@main.command()
@click.option("--mock", is_flag=True, default=False, help="Use built-in mock simulator")
@click.option("--scenario", type=str, default="all", help="Fault scenario")
@click.option("--speed", type=float, default=1.0, help="Time compression multiplier")
def demo(**kwargs) -> None:
    """60-second automated showcase."""
    ...

# -- peers subgroup --

@main.group()
def peers() -> None:
    """Peer management."""
    ...

@peers.command(name="list")
def peers_list() -> None:
    """Show configured peers and their liveness."""
    ...

@peers.command()
@click.argument("host")
def ping(host: str) -> None:
    """Test connectivity to a specific peer."""
    ...

# -- config subgroup --

@main.group()
def config() -> None:
    """Configuration management."""
    ...

@config.command()
def show() -> None:
    """Print effective configuration."""
    ...

@config.command()
def validate() -> None:
    """Validate config file."""
    ...

@config.command()
def init() -> None:
    """Interactive first-time setup."""
    ...

# -- bmc subgroup --

@main.group()
def bmc() -> None:
    """BMC connectivity tools."""
    ...

@bmc.command()
def detect() -> None:
    """Run BMC auto-detection and print result."""
    ...

@bmc.command()
def test() -> None:
    """Test BMC connectivity and authentication."""
    ...

@bmc.command()
def inventory() -> None:
    """Print BMC hardware inventory."""
    ...

# -- skills subgroup --

@main.group()
def skills() -> None:
    """Skill management."""
    ...

@skills.command(name="list")
def skills_list() -> None:
    """List installed skills."""
    ...

@skills.command()
@click.argument("skill_name")
def test_skill(skill_name: str) -> None:
    """Dry-run a skill against current telemetry."""
    ...

@skills.command()
def validate_skills() -> None:
    """Validate all skill YAML files.

    Exit 0: all valid. Exit 4: validation errors.
    """
    ...

# -- version command --

@main.command()
def version() -> None:
    """Print version info."""
    ...
```

**Exceptions raised:** CLI commands catch all exceptions and map to exit codes.
**Dependencies:** `click`, all other modules (composition root for one-shot commands).

---

## 3. Error Type Hierarchy

All custom exceptions inherit from `HarkenIQError`. Subclasses carry structured context for logging and diagnostics.

```python
class HarkenIQError(Exception):
    """Base exception for all HarkenIQ agent errors."""
    pass


# -- Configuration Errors --

class ConfigError(HarkenIQError):
    """Configuration loading, parsing, or validation failure.

    Examples:
      - Missing required config key
      - Invalid config value type
      - Credential file missing or undecryptable
      - Agent secret file missing
    """
    pass


# -- Redfish Communication Errors --

class RedfishError(HarkenIQError):
    """Base class for all Redfish BMC communication errors."""
    pass

class RedfishConnectionError(RedfishError):
    """TCP/TLS connection failure to the BMC.

    Attributes:
        host: BMC host that was unreachable.
        port: BMC port.
        cause: Underlying exception (e.g., aiohttp.ClientConnectorError).
    """
    def __init__(self, host: str, port: int, cause: Optional[Exception] = None):
        self.host = host
        self.port = port
        self.cause = cause
        super().__init__(f"Cannot connect to BMC at {host}:{port}: {cause}")

class RedfishAuthError(RedfishError):
    """BMC authentication failure (HTTP 401/403).

    Attributes:
        host: BMC host.
        status_code: HTTP status code (401 or 403).
    """
    def __init__(self, host: str, status_code: int):
        self.host = host
        self.status_code = status_code
        super().__init__(f"BMC authentication failed at {host}: HTTP {status_code}")

class RedfishTimeoutError(RedfishError):
    """Redfish HTTP request timed out.

    Attributes:
        host: BMC host.
        endpoint: Redfish URI that timed out.
        timeout_seconds: Configured timeout value.
    """
    def __init__(self, host: str, endpoint: str, timeout_seconds: float):
        self.host = host
        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds
        super().__init__(f"Redfish request to {host}{endpoint} timed out after {timeout_seconds}s")

class RedfishResponseError(RedfishError):
    """Unexpected HTTP response from BMC (status >= 400, except 401/404/503).

    Attributes:
        host: BMC host.
        endpoint: Redfish URI.
        status_code: HTTP status code.
        body: Response body (truncated to 500 chars).
    """
    def __init__(self, host: str, endpoint: str, status_code: int, body: str = ""):
        self.host = host
        self.endpoint = endpoint
        self.status_code = status_code
        self.body = body[:500]
        super().__init__(f"Redfish error at {host}{endpoint}: HTTP {status_code}")

class UnsupportedVendorError(RedfishError):
    """BMC vendor not recognized (neither Dell nor HPE OEM namespace found)."""
    pass


# -- Skill Errors --

class SkillError(HarkenIQError):
    """Base class for skill loading, parsing, and evaluation errors."""
    pass

class SkillParseError(SkillError):
    """Expression DSL syntax error.

    Attributes:
        condition: The condition string that failed to parse.
        detail: Description of the parse error.
        position: Character position where the error occurred (0-based).
    """
    def __init__(self, condition: str, detail: str, position: int = -1):
        self.condition = condition
        self.detail = detail
        self.position = position
        msg = f"Invalid expression '{condition}': {detail}"
        if position >= 0:
            msg += f" at position {position}"
        super().__init__(msg)

class SkillValidationError(SkillError):
    """Skill schema validation failure.

    Attributes:
        skill_name: Name of the skill (if available).
        errors: List of validation error messages.
    """
    def __init__(self, skill_name: str, errors: list[str]):
        self.skill_name = skill_name
        self.errors = errors
        super().__init__(f"Skill '{skill_name}' validation failed: {'; '.join(errors)}")

class SkillEvaluationError(SkillError):
    """Internal error during skill evaluation (should not occur with validated skills).

    Attributes:
        skill_name: Name of the skill.
        sensor_id: Sensor being evaluated.
        cause: Underlying exception.
    """
    def __init__(self, skill_name: str, sensor_id: str, cause: Exception):
        self.skill_name = skill_name
        self.sensor_id = sensor_id
        self.cause = cause
        super().__init__(f"Skill '{skill_name}' evaluation error on {sensor_id}: {cause}")


# -- Heartbeat Errors --

class HeartbeatError(HarkenIQError):
    """Base class for heartbeat protocol errors."""
    pass

class HeartbeatSendError(HeartbeatError):
    """Failed to send a UDP heartbeat packet.

    Attributes:
        target_host: Destination host.
        target_port: Destination port.
        cause: Underlying socket error.
    """
    def __init__(self, target_host: str, target_port: int, cause: Exception):
        self.target_host = target_host
        self.target_port = target_port
        self.cause = cause
        super().__init__(f"Heartbeat send to {target_host}:{target_port} failed: {cause}")

class HeartbeatReceiveError(HeartbeatError):
    """Failed to process a received heartbeat (corrupt, invalid HMAC, etc.).

    Attributes:
        source_addr: Source address of the bad packet.
        reason: Why the packet was rejected.
    """
    def __init__(self, source_addr: tuple[str, int], reason: str):
        self.source_addr = source_addr
        self.reason = reason
        super().__init__(f"Invalid heartbeat from {source_addr[0]}:{source_addr[1]}: {reason}")

class HeartbeatHmacError(HeartbeatError):
    """HMAC verification failure on a received heartbeat.

    Attributes:
        source_addr: Source address.
    """
    def __init__(self, source_addr: tuple[str, int]):
        self.source_addr = source_addr
        super().__init__(f"HMAC verification failed for heartbeat from {source_addr[0]}:{source_addr[1]}")


# -- Checkpoint Errors --

class CheckpointError(HarkenIQError):
    """Base class for state persistence errors."""
    pass

class CheckpointWriteError(CheckpointError):
    """Failed to write checkpoint to SQLite.

    Attributes:
        db_path: Path to the database file.
        cause: Underlying sqlite3 error.
    """
    def __init__(self, db_path: str, cause: Exception):
        self.db_path = db_path
        self.cause = cause
        super().__init__(f"Checkpoint write failed at {db_path}: {cause}")

class CheckpointReadError(CheckpointError):
    """Failed to read checkpoint from SQLite.

    Attributes:
        db_path: Path to the database file.
        cause: Underlying sqlite3 error.
    """
    def __init__(self, db_path: str, cause: Exception):
        self.db_path = db_path
        self.cause = cause
        super().__init__(f"Checkpoint read failed at {db_path}: {cause}")

class CheckpointCorruptError(CheckpointError):
    """Checkpoint database is corrupt or has an incompatible schema.

    Attributes:
        db_path: Path to the database file.
        detail: Description of the corruption.
    """
    def __init__(self, db_path: str, detail: str):
        self.db_path = db_path
        self.detail = detail
        super().__init__(f"Corrupt checkpoint at {db_path}: {detail}")


# -- Action Errors --

class ActionError(HarkenIQError):
    """Base class for action execution errors."""
    pass

class ActionExecutionError(ActionError):
    """Action failed during execution (Redfish PATCH/POST failed).

    Attributes:
        action_id: UUID of the failed action.
        action_type: ActionType that failed.
        cause: Underlying exception.
    """
    def __init__(self, action_id: str, action_type: str, cause: Exception):
        self.action_id = action_id
        self.action_type = action_type
        self.cause = cause
        super().__init__(f"Action {action_id} ({action_type}) execution failed: {cause}")

class ActionTimeoutError(ActionError):
    """Action execution timed out.

    Attributes:
        action_id: UUID of the timed-out action.
        timeout_seconds: Configured timeout.
    """
    def __init__(self, action_id: str, timeout_seconds: float):
        self.action_id = action_id
        self.timeout_seconds = timeout_seconds
        super().__init__(f"Action {action_id} timed out after {timeout_seconds}s")

class ActionUnauthorizedError(ActionError):
    """Action was attempted without proper authorization.

    Attributes:
        action_id: UUID of the unauthorized action.
    """
    def __init__(self, action_id: str):
        self.action_id = action_id
        super().__init__(f"Action {action_id} executed without authorization")
```

---

## 4. Data Flow Trace

Complete end-to-end trace for a single sensor poll cycle, from BMC HTTP response to TUI display and gRPC report.

### 4.1 Flow Diagram

```
 BMC (iDRAC/iLO)
      │
      │  GET /redfish/v1/Chassis/{id}/Thermal
      │
      ▼
 ┌────────────────────┐
 │ redfish.client.get  │  Returns raw JSON dict
 │  (aiohttp HTTPS)    │
 └────────┬───────────┘
          │ raw_data: dict[str, Any]
          ▼
 ┌────────────────────┐
 │ poller.poll_sensors │  Wraps response in SensorReading
 │                     │  Sets collected_at timestamp
 └────────┬───────────┘
          │ SensorReading (endpoint, raw_data, sensor_type, collected_at)
          ▼
 ┌──────────────────────────────┐
 │ redfish.normalize             │  Routes to dell.py or hpe.py based on DeviceIdentity
 │  normalize_fans(raw, ident)   │  Extracts fields per mapping tables (Doc 8, Section 4)
 │  normalize_thermals(raw, id.) │  Applies defaults for missing fields
 │                               │  Preserves OEM fields in oem_data dict
 └────────┬─────────────────────┘
          │ list[NormalizedFan], list[NormalizedThermal], ...
          ▼
 ┌──────────────────────────────┐
 │ skills.trending               │
 │  update_baseline(sensor_id,   │  For each sensor with a numeric primary field:
 │    value, timestamp, health)  │    1. Evict oldest sample if ring buffer full (remove_welford)
 │                               │    2. Add new sample (update_welford)
 │                               │    3. Update regression state (add/remove running sums)
 │                               │    4. Update min/max
 │                               │    5. Skip if health == CRITICAL (freeze baseline)
 └────────┬─────────────────────┘
          │ Updated Baseline objects (with new mean, stddev, confidence)
          ▼
 ┌──────────────────────────────┐
 │ skills.engine.evaluate        │  For each (skill, sensor) pair where skill.target matches:
 │                               │
 │  1. Check baseline confidence │  confidence < 0.5 → BMC health passthrough only
 │                               │  confidence >= 0.5 → expression eval enabled
 │  2. Build context dict        │  sensor fields + baseline_mean + baseline_stddev + deviation
 │                               │
 │  3. Evaluate each rule        │  For each SkillRule in order:
 │     expression.evaluate(      │    Parse AST evaluated against context dict
 │       rule.parsed_ast,        │    Type coercion rules applied (None → False)
 │       context)                │    If match: create Evidence with field snapshot
 │                               │
 │  4. Highest severity wins     │  CRITICAL > WARNING > TRENDING > HEALTHY > UNKNOWN
 │                               │
 │  5. Evaluate trending rules   │  Only if confidence == 1.0:
 │     trending.compute_trend()  │    Check slope, R², direction, time-to-threshold
 │                               │    Produce TrendResult if criteria met
 └────────┬─────────────────────┘
          │ Raw severity + Evidence list + TrendResult list
          ▼
 ┌──────────────────────────────┐
 │ skills.engine._apply_debounce │  N-of-M sliding window per (sensor_id, skill_name):
 │                               │
 │  HEALTHY → WARNING:  3 of 5   │  Append raw_severity to window
 │  HEALTHY → CRITICAL: 2 of 3   │  Count matches of target severity in window
 │  WARNING → CRITICAL: 2 of 3   │  Transition only if count >= threshold
 │  * → HEALTHY:        3 of 3   │
 │  * → TRENDING:    no debounce  │  Trending bypasses debounce (already statistical)
 └────────┬─────────────────────┘
          │ Final debounced Verdict objects
          ▼
 ┌─────────────────────────────────────────────────────────────┐
 │ Three parallel consumers receive each Verdict:              │
 │                                                             │
 │  ┌─────────────────────────┐                                │
 │  │ state.checkpoint         │  Accumulates dirty verdicts   │
 │  │  save_checkpoint()       │  Writes to SQLite on next     │
 │  │                          │  checkpoint cycle (600s)      │
 │  └─────────────────────────┘                                │
 │                                                             │
 │  ┌─────────────────────────┐                                │
 │  │ reporting.console        │  update_verdicts() →          │
 │  │  (TUI)                   │  Re-renders subsystem table   │
 │  │                          │  add_event() for transitions  │
 │  │                          │  update_learning_status()     │
 │  │                          │  update_trending()            │
 │  └─────────────────────────┘                                │
 │                                                             │
 │  ┌─────────────────────────┐                                │
 │  │ reporting.grpc_stub      │  report_verdict() →           │
 │  │  (Site Manager)          │  gRPC VerdictReport to SM     │
 │  │                          │  No-op if standalone mode     │
 │  └─────────────────────────┘                                │
 │                                                             │
 │  ┌─────────────────────────┐                                │
 │  │ state.machine            │  transition(EVALUATING →      │
 │  │                          │    DECIDING → OBSERVING)      │
 │  │                          │  Or → AWAITING_AUTH if action  │
 │  └─────────────────────────┘                                │
 └─────────────────────────────────────────────────────────────┘
```

### 4.2 Detailed Step Sequence

| Step | Module | Input | Output | Timing |
|------|--------|-------|--------|--------|
| 1 | `redfish.client.get()` | Redfish URI | Raw JSON dict | ~200--1500ms per endpoint |
| 2 | `poller.poll_sensors()` | RedfishClient, DeviceIdentity | SensorReading | Wraps step 1, polls ~5 endpoints |
| 3a | `normalize.normalize_fans()` | `thermal_data`, `identity` | `list[NormalizedFan]` | <1ms |
| 3b | `normalize.normalize_thermals()` | `thermal_data`, `identity` | `list[NormalizedThermal]` | <1ms |
| 3c | `normalize.normalize_psus()` | `power_data`, `identity` | `list[NormalizedPSU]` | <1ms |
| 3d | `normalize.normalize_disks()` | `storage_data`, `identity` | `list[NormalizedDisk]` | <1ms |
| 3e | `normalize.normalize_memory()` | `memory_data`, `metrics`, `identity` | `list[NormalizedMemory]` | <1ms |
| 3f | `normalize.normalize_health_rollup()` | `system_data`, `identity`, collections | `HealthRollup` | <1ms |
| 4 | `trending.update_baseline()` | `sensor_id`, `value`, `timestamp`, `health` | Updated `Baseline` | O(1) per sensor |
| 5 | `engine.evaluate()` | `NormalizedDevice`, baselines | `list[Verdict]` | <5ms for 50 sensors x 5 skills |
| 6 | `engine._apply_debounce()` | `sensor_id`, `skill_name`, raw severity | Debounced severity | O(1) per verdict |
| 7a | `checkpoint` buffer | Verdict | Queued for next write | Immediate |
| 7b | `console.update_verdicts()` | `list[Verdict]` | TUI refresh | <10ms |
| 7c | `grpc_stub.report_verdict()` | Verdict | gRPC call | ~5--50ms |
| 8 | `machine.transition()` | Target state | `StateTransition` | <1ms |

### 4.3 Total Cycle Budget

| Phase | Duration | Notes |
|-------|----------|-------|
| Redfish HTTP requests | 500--3000ms | 5 endpoints, partially parallelized |
| Normalization | <5ms | Pure data transformation |
| Baseline update | <1ms | O(1) Welford per sensor |
| Skill evaluation | <5ms | 50 sensors x 5 skills, AST evaluation |
| Debounce | <1ms | O(1) per verdict |
| TUI update | <10ms | rich library rendering |
| gRPC report | 5--50ms | Network to Site Manager |
| **Total** | **~600--3100ms** | Well under 60-second poll interval |

---

## 5. Async Task Coordination

### 5.1 Task Architecture

The agent runs three persistent asyncio tasks, each in an infinite loop with `asyncio.sleep()` between iterations:

```python
# In agent.py start():
async with asyncio.TaskGroup() as tg:
    tg.create_task(self._run_poller_loop())      # Sensor/log/inventory polling
    tg.create_task(self._run_heartbeat_loop())    # UDP send/receive + liveness check
    tg.create_task(self._run_report_loop())       # gRPC reporting to Site Manager
```

### 5.2 Poller Task Failure

**Scenario:** `redfish.client.get()` raises `RedfishConnectionError` or `RedfishTimeoutError` during a sensor poll.

**Behavior:**

1. The exception is caught inside `_run_poller_loop()`. It does NOT propagate to the TaskGroup.
2. The current poll cycle is abandoned. Partial data from already-completed endpoints is discarded (a poll cycle is all-or-nothing for data consistency).
3. The failure is logged at ERROR level: `"Sensor poll failed: {error}. Will retry in {interval}s."`
4. The agent state remains OBSERVING (the transition to EVALUATING is not attempted).
5. The loop sleeps for the configured `polling.sensor_interval` (60s) and retries.
6. A consecutive failure counter increments. After 5 consecutive failures:
   - Log at ERROR: `"BMC unreachable for {count} consecutive polls."`
   - TUI displays a BMC connectivity warning.
   - Heartbeat packets include degraded health summary.
7. On the first successful poll after failures, the counter resets and the TUI warning clears.

**State machine impact:** None. The agent stays in OBSERVING and the failed poll is invisible to the state machine.

```python
async def _run_poller_loop(self) -> None:
    consecutive_failures = 0
    while not self._shutdown_event.is_set():
        try:
            self._state_machine.transition(AgentState.EVALUATING, "Poll cycle starting")
            device = await self._poller.poll_sensors()
            # ... normalize, baseline, evaluate, debounce, emit verdicts ...
            self._state_machine.transition(AgentState.DECIDING, "Evaluation complete")
            # ... decide on actions ...
            self._state_machine.transition(AgentState.REPORTING, "Reporting")
            # ... report verdicts ...
            self._state_machine.transition(AgentState.OBSERVING, "Cycle complete")
            consecutive_failures = 0
        except RedfishError as e:
            consecutive_failures += 1
            logger.error("Sensor poll failed: %s (attempt %d)", e, consecutive_failures)
            if consecutive_failures >= 5:
                logger.error("BMC unreachable for %d consecutive polls", consecutive_failures)
            # Remain in OBSERVING state
            if self._state_machine.current_state != AgentState.OBSERVING:
                self._state_machine.transition(AgentState.OBSERVING, f"Poll failed: {e}")
        except Exception as e:
            logger.exception("Unexpected error in poller loop: %s", e)
            consecutive_failures += 1

        await asyncio.sleep(self._config["polling"]["sensor_interval"])
```

### 5.3 Heartbeat Send Failure

**Scenario:** `HeartbeatProtocol.send_heartbeat()` raises `HeartbeatSendError` (UDP socket error).

**Behavior:**

1. The exception is caught inside `_run_heartbeat_loop()`.
2. Logged at WARNING: `"Heartbeat send to {peer_host}:{peer_port} failed: {error}"`
3. The send is skipped for this peer. Other peers still receive heartbeats in the same cycle.
4. No retry within the same cycle. The next heartbeat cycle (10s later) will attempt the send again.
5. If the UDP socket itself is broken (bind error), the heartbeat task logs ERROR and attempts to rebind the socket on the next cycle.
6. The heartbeat task never propagates exceptions to the TaskGroup.

### 5.4 gRPC Report Failure

**Scenario:** gRPC connection to Site Manager drops or `report_verdict()` fails.

**Behavior:**

1. Connection failure triggers exponential backoff: 5s, 10s, 30s, 60s, max 300s.
2. Failed verdict reports are queued in-memory (bounded queue, max 1000 entries). Oldest entries are dropped if the queue overflows.
3. On reconnection, queued verdicts are drained in FIFO order.
4. In standalone mode (`site_manager.host` is empty), all gRPC calls are no-ops. No queue, no retries, no errors.
5. gRPC failures never affect the poller or heartbeat tasks.
6. The TUI displays "Site Manager: disconnected (retrying in Xs)" when connection is down.

```python
async def _run_report_loop(self) -> None:
    backoff = ExponentialBackoff(initial=5, maximum=300)
    while not self._shutdown_event.is_set():
        if self._grpc_reporter.is_standalone:
            await asyncio.sleep(60)
            continue
        try:
            if not self._grpc_reporter.is_connected:
                await self._grpc_reporter.connect()
                backoff.reset()
                # Drain queued verdicts
                while self._verdict_queue:
                    verdict = self._verdict_queue.popleft()
                    await self._grpc_reporter.report_verdict(verdict)
            # Send periodic heartbeat
            await self._grpc_reporter.send_heartbeat(...)
        except Exception as e:
            logger.warning("gRPC report failed: %s. Retry in %ds.", e, backoff.current)
            await asyncio.sleep(backoff.next())
        else:
            await asyncio.sleep(60)
```

### 5.5 Concurrent State Transitions

**Problem:** The poller and heartbeat tasks both run concurrently. State transitions must be serialized to prevent inconsistent state.

**Solution:** The `StateMachine` is protected by an `asyncio.Lock`. All `transition()` calls acquire the lock before modifying state. Since asyncio is single-threaded, this is primarily a safety measure against interleaved coroutine switches at `await` points within a transition sequence.

```python
class StateMachine:
    def __init__(self) -> None:
        self._state = AgentState.BOOTING
        self._lock = asyncio.Lock()
        self._history: list[StateTransition] = []

    async def transition(self, to_state: AgentState, reason: str) -> StateTransition:
        async with self._lock:
            if to_state not in self.TRANSITIONS[self._state]:
                raise ValueError(
                    f"Invalid transition: {self._state.value} -> {to_state.value}"
                )
            record = StateTransition(
                from_state=self._state,
                to_state=to_state,
                reason=reason,
                timestamp=datetime.utcnow().isoformat() + "Z",
            )
            self._state = to_state
            self._history.append(record)
            return record
```

**Note:** The heartbeat task does not trigger state transitions. Only the poller loop drives the state machine through the OBSERVING -> EVALUATING -> DECIDING -> ... cycle. The heartbeat and reporting tasks operate independently of the state machine. This eliminates contention in practice.

### 5.6 Task Restart Policy After Failure

If an asyncio task exits unexpectedly (an unhandled exception escapes the try/except in the loop body):

1. `asyncio.TaskGroup` cancels all sibling tasks and raises `ExceptionGroup`.
2. The `Agent.start()` method catches `ExceptionGroup`, logs the cause, and performs a graceful shutdown.
3. systemd detects process exit (non-zero exit code) and restarts the agent per `Restart=on-failure` with `RestartSec=5`.
4. On restart, the agent enters BOOTING, loads the last checkpoint, and resumes.

**Design rationale:** Tasks should never exit their loop. All expected exceptions (Redfish, heartbeat, gRPC) are caught inside the loop. An unexpected exception indicates a bug, and a full process restart is the safest recovery. This is simpler and more predictable than in-process task restart, which could leave shared state inconsistent.

### 5.7 Graceful Shutdown Sequence

Triggered by SIGTERM (systemd stop) or SIGINT (Ctrl+C).

```
Signal received (SIGTERM/SIGINT)
      │
      ▼
  1. Set self._shutdown_event
      │  All task loops check this event and exit their while loop
      │
      ▼
  2. Cancel in-progress Redfish request (3s timeout)
      │  await asyncio.wait_for(current_poll, timeout=3.0)
      │  If timeout: force-cancel the aiohttp request
      │
      ▼
  3. Send final heartbeat to all peers
      │  HeartbeatPacket with state="SHUTTING_DOWN"
      │  Best-effort: failure is logged, not retried
      │
      ▼
  4. Force checkpoint
      │  await checkpoint_manager.save_checkpoint(...)
      │  Flushes all dirty baselines, verdicts, peer state
      │
      ▼
  5. Close Redfish session
      │  await client.delete_session()
      │  Best-effort: failure logged (BMC will timeout the session anyway)
      │
      ▼
  6. Close UDP socket
      │  transport.close()
      │
      ▼
  7. Close gRPC channel
      │  await grpc_reporter.close()
      │
      ▼
  8. Close checkpoint database
      │  await checkpoint_manager.close()
      │
      ▼
  9. Stop TUI (if running)
      │  await console_reporter.stop()
      │
      ▼
  10. Exit process with code 0
```

**Timeout guarantee:** The entire shutdown sequence must complete within 10 seconds. If any step hangs beyond this, the systemd `WatchdogSec=120` will eventually SIGKILL the process. However, the agent's internal shutdown timeout (10s) should prevent this by force-canceling any remaining tasks after the deadline.

```python
async def stop(self) -> None:
    self._shutdown_event.set()
    try:
        async with asyncio.timeout(10):
            # Steps 2-9 above
            ...
    except asyncio.TimeoutError:
        logger.error("Shutdown timed out after 10s, forcing exit")
    finally:
        logger.info("Agent stopped")
```

---

## 6. Cross-Cutting Concerns

### 6.1 Sensor ID Convention

All modules use a consistent sensor ID format:

```
{sensor_type}:{sensor_name}
```

Examples:
- `fan:System Board Fan1A`
- `disk:Disk.Bay.2`
- `memory:DIMM.Socket.A1`
- `psu:PSU.Slot.1`
- `thermal:Inlet Temp`

The sensor ID is constructed by the normalizer and used as the primary key for baselines, verdicts, debounce state, and checkpoint tables.

### 6.2 Timestamp Convention

All timestamps in domain objects use ISO 8601 format with UTC timezone:

```
2026-09-15T14:30:00.123Z
```

Internal calculations (regression, baseline timing) use Unix timestamps (`time.time()`) for arithmetic convenience. Conversion to ISO 8601 happens at the domain object boundary.

### 6.3 Configuration Precedence

Per Doc 6, Section 5.3, all modules that accept configuration follow this precedence:

```
CLI flags > environment variables > config.yaml > hardcoded defaults
```

The `Agent.__init__()` method resolves this precedence once and passes the final effective config dict to all submodules.

### 6.4 Logging Convention

All modules log using Python's `logging` module with a per-module logger:

```python
import logging
logger = logging.getLogger(__name__)
```

Log format is JSON Lines to `/var/log/harkeniq/agent.log` (Doc 6, Section 8.1). The log handler is configured once in `Agent.__init__()`.
