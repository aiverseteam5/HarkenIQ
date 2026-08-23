"""HarkenIQ domain objects: enums, dataclasses, and AST node types.

All domain objects used across modules are defined here. Normalized sensor
models (NormalizedFan, NormalizedDisk, etc.) are in harkeniq.redfish.normalize
per Doc 08. This module covers everything else from Doc 10 §1.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Union


# ---------------------------------------------------------------------------
# Enums (Doc 10 §1.1)
# ---------------------------------------------------------------------------


class VerdictSeverity(Enum):
    """Verdict severity with total ordering.

    UNKNOWN < HEALTHY < TRENDING < WARNING < CRITICAL.
    Highest severity wins when multiple rules match the same sensor.
    """

    UNKNOWN = "UNKNOWN"
    HEALTHY = "HEALTHY"
    TRENDING = "TRENDING"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"

    _ORDER = None  # placeholder for class-level ordering list

    def __lt__(self, other: VerdictSeverity) -> bool:
        return _VERDICT_ORDER.index(self) < _VERDICT_ORDER.index(other)

    def __le__(self, other: VerdictSeverity) -> bool:
        return self == other or self < other

    def __gt__(self, other: VerdictSeverity) -> bool:
        return not self <= other

    def __ge__(self, other: VerdictSeverity) -> bool:
        return not self < other


_VERDICT_ORDER = [
    VerdictSeverity.UNKNOWN,
    VerdictSeverity.HEALTHY,
    VerdictSeverity.TRENDING,
    VerdictSeverity.WARNING,
    VerdictSeverity.CRITICAL,
]


class AgentState(Enum):
    """Agent state machine states (Doc 06 §11)."""

    BOOTING = "BOOTING"
    OBSERVING = "OBSERVING"
    EVALUATING = "EVALUATING"
    DECIDING = "DECIDING"
    AWAITING_AUTH = "AWAITING_AUTH"
    ACTING = "ACTING"
    REPORTING = "REPORTING"


class PeerStatus(Enum):
    """Peer liveness state (Doc 06 §9.3)."""

    UNKNOWN = "UNKNOWN"
    ALIVE = "ALIVE"
    UNRESPONSIVE = "UNRESPONSIVE"


class ActionType(Enum):
    """Action allow-list (Doc 06 §11A.1, spec A2.1)."""

    # R1 actions (locally authorized)
    IDENTIFY_LED = "IDENTIFY_LED"
    COLLECT_DIAGNOSTICS = "COLLECT_DIAGNOSTICS"
    FAN_RESET = "FAN_RESET"
    # R3a actions (SM-authorized, budget-gated)
    SEL_CLEAR = "SEL_CLEAR"
    BMC_RESET = "BMC_RESET"
    POWER_CYCLE = "POWER_CYCLE"
    POWER_CAP_ADJUST = "POWER_CAP_ADJUST"


class ActionStatus(Enum):
    """Action lifecycle states (Doc 06 §11A.2)."""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    DENIED = "DENIED"
    EXECUTING = "EXECUTING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


# ---------------------------------------------------------------------------
# Sensor Reading — raw Redfish data (Doc 10 §1.2)
# ---------------------------------------------------------------------------


@dataclass
class SensorReading:
    """Raw sensor reading from a single Redfish GET, before normalization."""

    endpoint: str
    raw_data: dict[str, Any]
    sensor_type: str  # fan | disk | memory | psu | thermal | log | inventory
    collected_at: str  # ISO 8601
    http_status: int = 200
    response_time_ms: float = 0.0


# ---------------------------------------------------------------------------
# Expression DSL AST nodes (Doc 10 §1.6, Doc 07 §9.2)
# ---------------------------------------------------------------------------

ASTNode = Union["Comparison", "BooleanOp", "NotOp"]


@dataclass
class Comparison:
    """Leaf AST node: compares a field to a value."""

    field: str
    operator: str  # ==, !=, <, >, <=, >=
    value: Any  # literal number, string, boolean, or field reference


@dataclass
class BooleanOp:
    """Internal AST node: AND or OR."""

    op: str  # AND | OR
    left: ASTNode
    right: ASTNode


@dataclass
class NotOp:
    """Internal AST node: logical negation."""

    operand: ASTNode


# ---------------------------------------------------------------------------
# Debounce (Doc 10 §1.4, Doc 13 §4.3)
# ---------------------------------------------------------------------------


@dataclass
class DebounceConfig:
    """Per-rule debounce override. If absent, global defaults apply."""

    count: int  # N: matches required
    window: int  # M: consecutive polls to examine


@dataclass
class DebounceState:
    """Tracks N-of-M debounce for a (sensor_id, skill_name) pair."""

    sensor_id: str
    skill_name: str
    window: list[VerdictSeverity] = field(default_factory=list)
    window_size: int = 3
    threshold_count: int = 2
    current_debounced: VerdictSeverity = VerdictSeverity.HEALTHY
    last_transition_at: Optional[str] = None


# ---------------------------------------------------------------------------
# Skill definitions (Doc 10 §1.6)
# ---------------------------------------------------------------------------


@dataclass
class ActionRecommendation:
    """Action recommendation attached to a skill rule."""

    type: ActionType
    params: dict[str, str] = field(default_factory=dict)


@dataclass
class SkillRule:
    """A single detection rule within a skill."""

    condition: str  # raw condition string from YAML
    parsed_ast: ASTNode  # parsed expression tree
    verdict: VerdictSeverity
    message_template: str
    debounce: Optional[DebounceConfig] = None
    action: Optional[ActionRecommendation] = None


@dataclass
class TrendingRule:
    """A trending detection rule within a skill."""

    field: str  # normalized field to track
    direction: str  # declining | rising
    verdict: VerdictSeverity  # always TRENDING
    message_template: str
    threshold_field: Optional[str] = None


@dataclass
class SkillDefinition:
    """Complete parsed skill loaded from a YAML file."""

    name: str
    version: int
    target: str  # fan | disk | memory | psu | thermal
    description: str
    rules: list[SkillRule]
    trending: list[TrendingRule] = field(default_factory=list)
    default_verdict: VerdictSeverity = VerdictSeverity.HEALTHY


# ---------------------------------------------------------------------------
# Evidence and Verdict (Doc 10 §1.4-1.5)
# ---------------------------------------------------------------------------


@dataclass
class Evidence:
    """Snapshot of sensor data that caused a rule to match."""

    sensor_id: str
    skill_name: str
    rule_index: int
    condition: str
    fields: dict[str, Any]
    timestamp: str
    baseline_confidence: float = 0.0


@dataclass
class Verdict:
    """Result of evaluating one skill against one sensor."""

    sensor_id: str
    skill_name: str
    severity: VerdictSeverity
    message: str
    evidence: list[Evidence] = field(default_factory=list)
    timestamp: str = ""
    debounce_state: Optional[DebounceState] = None


# ---------------------------------------------------------------------------
# Peer and Heartbeat (Doc 10 §1.7)
# ---------------------------------------------------------------------------


@dataclass
class Peer:
    """Tracked peer agent state."""

    peer_id: str
    host: str
    port: int
    last_heartbeat: Optional[str] = None
    status: PeerStatus = PeerStatus.UNKNOWN
    last_known_health: Optional[dict[str, str]] = None
    health_buffer: list[dict[str, str]] = field(default_factory=list)
    name: Optional[str] = None


@dataclass
class HeartbeatPacket:
    """UDP heartbeat packet (Doc 06 §9.2). Max ~576 bytes with HMAC."""

    v: int  # protocol version (1)
    agent_id: str
    name: str
    seq: int
    ts: float  # unix timestamp
    state: str  # AgentState value
    health_summary: dict[str, str]
    hmac: str = ""


# ---------------------------------------------------------------------------
# State Machine (Doc 10 §1.8)
# ---------------------------------------------------------------------------


@dataclass
class StateTransition:
    """Record of an agent state transition."""

    from_state: AgentState
    to_state: AgentState
    reason: str
    timestamp: str


# ---------------------------------------------------------------------------
# Actions (Doc 10 §1.9)
# ---------------------------------------------------------------------------


@dataclass
class ActionOutcome:
    """Result of executing an action."""

    action_id: str
    type: ActionType
    target: str
    success: bool
    error_message: Optional[str] = None
    duration_ms: float = 0.0
    timestamp: str = ""


@dataclass
class Action:
    """A proposed remediation action in the approval pipeline."""

    id: str
    type: ActionType
    params: dict[str, str] = field(default_factory=dict)
    status: ActionStatus = ActionStatus.PENDING
    sensor_id: str = ""
    skill_name: str = ""
    verdict_severity: VerdictSeverity = VerdictSeverity.UNKNOWN
    proposed_at: str = ""
    approved_at: Optional[str] = None
    completed_at: Optional[str] = None
    outcome: Optional[ActionOutcome] = None


# ---------------------------------------------------------------------------
# Baseline and Trending (Doc 10 §1.10, Doc 13)
# ---------------------------------------------------------------------------


@dataclass
class RegressionState:
    """Incremental OLS regression state (Doc 13 §3.2)."""

    sum_x: float = 0.0
    sum_y: float = 0.0
    sum_xy: float = 0.0
    sum_x2: float = 0.0
    sum_y2: float = 0.0
    n: int = 0
    eviction_count: int = 0


@dataclass
class Baseline:
    """Per-sensor baseline statistics using Welford's online algorithm."""

    sensor_id: str
    mean: float = 0.0
    stddev: float = 0.0
    variance: float = 0.0
    m2: float = 0.0  # Welford's M2 accumulator
    min_val: float = math.inf
    max_val: float = -math.inf
    sample_count: int = 0
    ring_buffer: list[tuple[float, float]] = field(default_factory=list)
    buffer_size: int = 1440  # 24h at 60s polling
    first_sample_at: Optional[str] = None
    last_sample_at: Optional[str] = None
    degraded_baseline: bool = False
    critical_pause_remaining: int = 0
    regression_state: RegressionState = field(default_factory=RegressionState)

    @property
    def confidence(self) -> float:
        """Baseline confidence: 0.0 to 1.0. min_samples=60 default."""
        min_samples = 60
        if min_samples <= 0:
            return 1.0
        return min(1.0, self.sample_count / min_samples)


@dataclass
class TrendResult:
    """Output of trending analysis (Doc 13 §3.5)."""

    sensor_id: str
    field: str
    slope: float  # units per hour
    r_squared: float
    direction: str  # rising | declining
    current_value: float
    threshold_name: str
    threshold_value: float
    time_to_threshold_hours: float
    confidence: float
    message: str


# ---------------------------------------------------------------------------
# Log Entry (Doc 10 §1.11)
# ---------------------------------------------------------------------------


@dataclass
class LogEntry:
    """Normalized hardware event log entry."""

    id: str = ""
    timestamp: str = ""
    severity: str = ""  # OK | Warning | Critical
    message: str = ""
    message_id: str = ""
    component_id: Optional[str] = None  # Dell FQDD; None on HPE
    category: str = ""
    source: str = ""  # sel (Dell) | iml (HPE)
