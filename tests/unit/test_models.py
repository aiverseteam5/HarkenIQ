"""Unit tests for domain objects (models.py and normalize.py)."""

from harkeniq.models import (
    Action,
    ActionOutcome,
    ActionRecommendation,
    ActionStatus,
    ActionType,
    AgentState,
    ASTNode,
    Baseline,
    BooleanOp,
    Comparison,
    DebounceConfig,
    DebounceState,
    Evidence,
    HeartbeatPacket,
    LogEntry,
    NotOp,
    Peer,
    PeerStatus,
    RegressionState,
    SensorReading,
    SkillDefinition,
    SkillRule,
    StateTransition,
    TrendingRule,
    TrendResult,
    Verdict,
    VerdictSeverity,
)
from harkeniq.redfish.normalize import (
    compute_health_rollup,
    DeviceIdentity,
    HealthRollup,
    NormalizedDevice,
    NormalizedDisk,
    NormalizedFan,
    NormalizedMemory,
    NormalizedPowerMetrics,
    NormalizedPSU,
    NormalizedThermal,
    worst_health,
)


# ---------------------------------------------------------------------------
# VerdictSeverity ordering
# ---------------------------------------------------------------------------


class TestVerdictSeverity:
    def test_ordering(self):
        assert VerdictSeverity.UNKNOWN < VerdictSeverity.HEALTHY
        assert VerdictSeverity.HEALTHY < VerdictSeverity.TRENDING
        assert VerdictSeverity.TRENDING < VerdictSeverity.WARNING
        assert VerdictSeverity.WARNING < VerdictSeverity.CRITICAL

    def test_equality(self):
        assert VerdictSeverity.CRITICAL == VerdictSeverity.CRITICAL
        assert not (VerdictSeverity.CRITICAL < VerdictSeverity.CRITICAL)

    def test_max_severity(self):
        verdicts = [VerdictSeverity.HEALTHY, VerdictSeverity.WARNING, VerdictSeverity.TRENDING]
        assert max(verdicts) == VerdictSeverity.WARNING

    def test_ge_le(self):
        assert VerdictSeverity.CRITICAL >= VerdictSeverity.WARNING
        assert VerdictSeverity.WARNING <= VerdictSeverity.CRITICAL
        assert VerdictSeverity.HEALTHY >= VerdictSeverity.HEALTHY


# ---------------------------------------------------------------------------
# Enum construction
# ---------------------------------------------------------------------------


class TestEnums:
    def test_agent_states(self):
        assert AgentState.BOOTING.value == "BOOTING"
        assert AgentState.AWAITING_AUTH.value == "AWAITING_AUTH"
        assert len(AgentState) == 7

    def test_peer_status(self):
        assert PeerStatus.ALIVE.value == "ALIVE"
        assert len(PeerStatus) == 3

    def test_action_type(self):
        assert ActionType.IDENTIFY_LED.value == "IDENTIFY_LED"
        assert len(ActionType) == 7  # R1: 3 + R3a: 4

    def test_action_status(self):
        assert ActionStatus.PENDING.value == "PENDING"
        assert len(ActionStatus) == 6


# ---------------------------------------------------------------------------
# Dataclass construction
# ---------------------------------------------------------------------------


class TestDataclasses:
    def test_sensor_reading(self):
        sr = SensorReading(
            endpoint="/redfish/v1/Chassis/System.Embedded.1/Thermal",
            raw_data={"Fans": []},
            sensor_type="fan",
            collected_at="2026-09-15T14:30:00Z",
        )
        assert sr.http_status == 200
        assert sr.response_time_ms == 0.0

    def test_evidence(self):
        ev = Evidence(
            sensor_id="fan:Fan1A",
            skill_name="fan-health",
            rule_index=0,
            condition="health == 'Critical'",
            fields={"health": "Critical"},
            timestamp="2026-09-15T14:30:00Z",
        )
        assert ev.baseline_confidence == 0.0

    def test_verdict(self):
        v = Verdict(
            sensor_id="fan:Fan1A",
            skill_name="fan-health",
            severity=VerdictSeverity.CRITICAL,
            message="Fan Fan1A has failed",
        )
        assert v.evidence == []
        assert v.debounce_state is None

    def test_peer(self):
        p = Peer(peer_id="uuid-123", host="10.0.1.101", port=5150)
        assert p.status == PeerStatus.UNKNOWN
        assert p.health_buffer == []

    def test_heartbeat_packet(self):
        hp = HeartbeatPacket(
            v=1,
            agent_id="uuid-456",
            name="rack-12-server-04",
            seq=1,
            ts=1726408200.0,
            state="OBSERVING",
            health_summary={"fan": "OK", "disk": "OK"},
        )
        assert hp.hmac == ""

    def test_action(self):
        a = Action(id="act-1", type=ActionType.IDENTIFY_LED)
        assert a.status == ActionStatus.PENDING
        assert a.outcome is None

    def test_action_outcome(self):
        ao = ActionOutcome(
            action_id="act-1",
            type=ActionType.IDENTIFY_LED,
            target="Disk.Bay.2",
            success=True,
        )
        assert ao.error_message is None

    def test_baseline_confidence(self):
        b = Baseline(sensor_id="fan:Fan1A", sample_count=30)
        assert b.confidence == 0.5

        b2 = Baseline(sensor_id="fan:Fan1A", sample_count=60)
        assert b2.confidence == 1.0

        b3 = Baseline(sensor_id="fan:Fan1A", sample_count=120)
        assert b3.confidence == 1.0

        b4 = Baseline(sensor_id="fan:Fan1A", sample_count=0)
        assert b4.confidence == 0.0

    def test_regression_state_defaults(self):
        rs = RegressionState()
        assert rs.n == 0
        assert rs.sum_x == 0.0

    def test_trend_result(self):
        tr = TrendResult(
            sensor_id="fan:Fan1A",
            field="speed_rpm",
            slope=-8.5,
            r_squared=0.87,
            direction="declining",
            current_value=9200,
            threshold_name="threshold_low_critical",
            threshold_value=480,
            time_to_threshold_hours=1027.1,
            confidence=1.0,
            message="Fan declining",
        )
        assert tr.slope < 0

    def test_state_transition(self):
        st = StateTransition(
            from_state=AgentState.BOOTING,
            to_state=AgentState.OBSERVING,
            reason="startup complete",
            timestamp="2026-09-15T14:30:00Z",
        )
        assert st.from_state == AgentState.BOOTING

    def test_log_entry(self):
        le = LogEntry()
        assert le.component_id is None
        assert le.source == ""

    def test_debounce_config(self):
        dc = DebounceConfig(count=2, window=3)
        assert dc.count == 2

    def test_debounce_state(self):
        ds = DebounceState(sensor_id="fan:Fan1A", skill_name="fan-health")
        assert ds.current_debounced == VerdictSeverity.HEALTHY


# ---------------------------------------------------------------------------
# AST node types
# ---------------------------------------------------------------------------


class TestASTNodes:
    def test_comparison(self):
        c = Comparison(field="health", operator="==", value="Critical")
        assert c.field == "health"

    def test_boolean_op(self):
        left = Comparison(field="health", operator="==", value="Critical")
        right = Comparison(field="state", operator="==", value="Enabled")
        b = BooleanOp(op="AND", left=left, right=right)
        assert b.op == "AND"

    def test_not_op(self):
        inner = Comparison(field="state", operator="==", value="Absent")
        n = NotOp(operand=inner)
        assert isinstance(n.operand, Comparison)


# ---------------------------------------------------------------------------
# Skill definitions
# ---------------------------------------------------------------------------


class TestSkillDefinitions:
    def test_skill_rule(self):
        ast = Comparison(field="health", operator="==", value="Critical")
        rule = SkillRule(
            condition="health == 'Critical'",
            parsed_ast=ast,
            verdict=VerdictSeverity.CRITICAL,
            message_template="Fan {name} has failed",
        )
        assert rule.action is None
        assert rule.debounce is None

    def test_skill_rule_with_action(self):
        ast = Comparison(field="health", operator="==", value="Critical")
        rule = SkillRule(
            condition="health == 'Critical'",
            parsed_ast=ast,
            verdict=VerdictSeverity.CRITICAL,
            message_template="Fan {name} has failed",
            action=ActionRecommendation(
                type=ActionType.COLLECT_DIAGNOSTICS,
                params={"reason": "Fan failure"},
            ),
        )
        assert rule.action.type == ActionType.COLLECT_DIAGNOSTICS

    def test_trending_rule(self):
        tr = TrendingRule(
            field="speed_rpm",
            direction="declining",
            verdict=VerdictSeverity.TRENDING,
            message_template="Fan {name} declining at {rate}/hr",
            threshold_field="threshold_low_critical",
        )
        assert tr.direction == "declining"

    def test_skill_definition(self):
        ast = Comparison(field="health", operator="==", value="Critical")
        sd = SkillDefinition(
            name="fan-health",
            version=1,
            target="fan",
            description="Detect fan failures",
            rules=[
                SkillRule(
                    condition="health == 'Critical'",
                    parsed_ast=ast,
                    verdict=VerdictSeverity.CRITICAL,
                    message_template="Fan {name} has failed",
                )
            ],
        )
        assert sd.default_verdict == VerdictSeverity.HEALTHY
        assert len(sd.rules) == 1


# ---------------------------------------------------------------------------
# Normalized models
# ---------------------------------------------------------------------------


class TestNormalizedModels:
    def test_device_identity(self):
        di = DeviceIdentity(vendor="dell", model="PowerEdge R750", controller_type="iDRAC", controller_version=9)
        assert di.vendor == "dell"

    def test_normalized_fan_defaults(self):
        f = NormalizedFan()
        assert f.health == "Unknown"
        assert f.speed_rpm is None
        assert f.oem_data == {}

    def test_normalized_disk_defaults(self):
        d = NormalizedDisk()
        assert d.smart_alert is False
        assert d.raid_status is None

    def test_normalized_memory_defaults(self):
        m = NormalizedMemory()
        assert m.alarm_ecc_correctable is False
        assert m.ecc_correctable_lifetime is None

    def test_normalized_psu_defaults(self):
        p = NormalizedPSU()
        assert p.redundancy_health is None

    def test_normalized_thermal_defaults(self):
        t = NormalizedThermal()
        assert t.reading_c is None
        assert t.threshold_fatal is None

    def test_normalized_device(self):
        dev = NormalizedDevice()
        assert dev.fans == []
        assert dev.power_metrics is None
        assert dev.health_rollup.overall == "Unknown"


# ---------------------------------------------------------------------------
# Health rollup
# ---------------------------------------------------------------------------


class TestHealthRollup:
    def test_worst_health_ok(self):
        assert worst_health(["OK", "OK", "OK"]) == "OK"

    def test_worst_health_warning(self):
        assert worst_health(["OK", "Warning", "OK"]) == "Warning"

    def test_worst_health_critical(self):
        assert worst_health(["OK", "Warning", "Critical"]) == "Critical"

    def test_worst_health_empty(self):
        assert worst_health([]) == "Unknown"

    def test_worst_health_unknown(self):
        assert worst_health(["Unknown"]) == "Unknown"

    def test_compute_health_rollup_all_ok(self):
        dev = NormalizedDevice(
            fans=[NormalizedFan(health="OK"), NormalizedFan(health="OK")],
            disks=[NormalizedDisk(health="OK")],
            memory=[NormalizedMemory(health="OK", state="Enabled")],
            psus=[NormalizedPSU(health="OK")],
            thermals=[NormalizedThermal(health="OK")],
        )
        rollup = compute_health_rollup(dev)
        assert rollup.overall == "OK"
        assert rollup.fan == "OK"

    def test_compute_health_rollup_fan_critical(self):
        dev = NormalizedDevice(
            fans=[NormalizedFan(health="OK"), NormalizedFan(health="Critical")],
            disks=[NormalizedDisk(health="OK")],
            memory=[],
            psus=[NormalizedPSU(health="OK")],
            thermals=[NormalizedThermal(health="OK")],
        )
        rollup = compute_health_rollup(dev)
        assert rollup.fan == "Critical"
        assert rollup.overall == "Critical"

    def test_compute_health_rollup_empty_subsystem(self):
        dev = NormalizedDevice()
        rollup = compute_health_rollup(dev)
        assert rollup.fan == "Unknown"
        assert rollup.overall == "Unknown"

    def test_compute_health_rollup_skips_absent_memory(self):
        dev = NormalizedDevice(
            fans=[NormalizedFan(health="OK")],
            disks=[NormalizedDisk(health="OK")],
            memory=[
                NormalizedMemory(health="OK", state="Enabled"),
                NormalizedMemory(health="Unknown", state="Absent"),
            ],
            psus=[NormalizedPSU(health="OK")],
            thermals=[NormalizedThermal(health="OK")],
        )
        rollup = compute_health_rollup(dev)
        assert rollup.memory == "OK"  # Absent DIMMs excluded
