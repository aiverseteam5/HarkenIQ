"""R3a P2/P3/P7: Resource profiles, expanded actions, OS signals, diagnosis.

Tests the resource monitoring + degradation, action preconditions + blast
radius + verification, OS signal parsing, and Diagnosis model.
"""

import time

import pytest

from harkeniq.autonomy.blast_radius import BlastRadiusLimiter, DEFAULT_LIMITS
from harkeniq.autonomy.diagnosis import ConfidenceDimension, Diagnosis, DiagnosisEvidence
from harkeniq.autonomy.preconditions import (
    ACTION_RISK,
    PreconditionResult,
    check_preconditions,
)
from harkeniq.autonomy.resources import (
    DegradationLevel,
    PROFILES,
    ResourceMonitor,
    ResourceSnapshot,
)
from harkeniq.autonomy.tier import TierLevel
from harkeniq.autonomy.verification import (
    ActionOutcome,
    OutcomeStatus,
    VERIFICATION_WINDOWS,
    evaluate_verification,
)
from harkeniq.models import ActionType
from harkeniq.os_signals.collector import OSEvent, OSSignalCollector, SignalSourceType
from harkeniq.os_signals.syslog import SyslogSource


# ===========================================================================
# P2: Resource Profiles
# ===========================================================================


class TestResourceProfiles:
    def test_three_profiles_exist(self):
        assert "constrained" in PROFILES
        assert "standard" in PROFILES
        assert "performance" in PROFILES

    def test_standard_defaults(self):
        p = PROFILES["standard"]
        assert p.memory_target_mb == 50
        assert p.memory_soft_mb == 75
        assert p.memory_hard_mb == 100
        assert p.cpu_target_pct == 5.0

    def test_constrained_is_tighter(self):
        c = PROFILES["constrained"]
        s = PROFILES["standard"]
        assert c.memory_hard_mb < s.memory_hard_mb
        assert c.cpu_hard_pct < s.cpu_hard_pct


class TestResourceMonitor:
    def test_normal_when_within_target(self):
        mon = ResourceMonitor("standard")
        snap = ResourceSnapshot(memory_rss_mb=30.0, cpu_percent=2.0, timestamp=time.time())
        level = mon.evaluate(snap)
        assert level == DegradationLevel.NORMAL
        assert mon.poll_interval_multiplier == 1.0

    def test_throttled_after_two_soft_hits(self):
        mon = ResourceMonitor("standard")
        snap = ResourceSnapshot(memory_rss_mb=80.0, cpu_percent=2.0, timestamp=time.time())
        mon.evaluate(snap)  # first soft hit
        level = mon.evaluate(snap)  # second soft hit -> throttled
        assert level == DegradationLevel.THROTTLED
        assert mon.poll_interval_multiplier == 2.0

    def test_degraded_on_hard_threshold(self):
        mon = ResourceMonitor("standard")
        snap = ResourceSnapshot(memory_rss_mb=110.0, cpu_percent=2.0, timestamp=time.time())
        level = mon.evaluate(snap)
        assert level == DegradationLevel.DEGRADED

    def test_observe_only_after_three_hard_hits(self):
        mon = ResourceMonitor("standard")
        snap = ResourceSnapshot(memory_rss_mb=110.0, cpu_percent=2.0, timestamp=time.time())
        mon.evaluate(snap)
        mon.evaluate(snap)
        level = mon.evaluate(snap)
        assert level == DegradationLevel.OBSERVE_ONLY
        assert mon.poll_interval_multiplier == 4.0

    def test_recovery_to_normal(self):
        mon = ResourceMonitor("standard")
        hard = ResourceSnapshot(memory_rss_mb=110.0, cpu_percent=2.0, timestamp=time.time())
        mon.evaluate(hard)
        mon.evaluate(hard)
        normal = ResourceSnapshot(memory_rss_mb=30.0, cpu_percent=2.0, timestamp=time.time())
        level = mon.evaluate(normal)
        assert level == DegradationLevel.NORMAL

    def test_health_fields_for_heartbeat(self):
        mon = ResourceMonitor("constrained")
        snap = ResourceSnapshot(memory_rss_mb=25.0, cpu_percent=1.5, timestamp=time.time())
        fields = mon.health_fields(snap)
        assert fields["resource_profile"] == "constrained"
        assert fields["resource_level"] == "normal"
        assert "25.0" in fields["memory_rss_mb"]

    def test_cpu_soft_triggers_throttle(self):
        mon = ResourceMonitor("standard")
        snap = ResourceSnapshot(memory_rss_mb=30.0, cpu_percent=8.0, timestamp=time.time())
        mon.evaluate(snap)
        level = mon.evaluate(snap)
        assert level == DegradationLevel.THROTTLED


# ===========================================================================
# P3: Expanded Actions - Preconditions
# ===========================================================================


class TestPreconditions:
    def test_r1_actions_have_no_preconditions(self):
        result = check_preconditions(ActionType.IDENTIFY_LED, {}, {})
        assert result.passed

    def test_sel_clear_requires_forwarded_events(self):
        result = check_preconditions(
            ActionType.SEL_CLEAR,
            {"sel_percent_full": 90},
            {"sel_events_forwarded": False},
        )
        assert not result.passed
        assert "forwarded" in result.reason.lower()

    def test_sel_clear_requires_80_percent_full(self):
        result = check_preconditions(
            ActionType.SEL_CLEAR,
            {"sel_percent_full": 50},
            {"sel_events_forwarded": True},
        )
        assert not result.passed
        assert "80%" in result.reason

    def test_sel_clear_passes_when_conditions_met(self):
        result = check_preconditions(
            ActionType.SEL_CLEAR,
            {"sel_percent_full": 95},
            {"sel_events_forwarded": True},
        )
        assert result.passed

    def test_bmc_reset_requires_3_poll_failures(self):
        result = check_preconditions(
            ActionType.BMC_RESET,
            {"firmware_update_in_progress": False},
            {"bmc_consecutive_poll_failures": 1},
        )
        assert not result.passed

    def test_bmc_reset_blocked_during_firmware_update(self):
        result = check_preconditions(
            ActionType.BMC_RESET,
            {"firmware_update_in_progress": True},
            {"bmc_consecutive_poll_failures": 5},
        )
        assert not result.passed
        assert "firmware" in result.reason.lower()

    def test_power_cycle_requires_t1_corroboration(self):
        result = check_preconditions(
            ActionType.POWER_CYCLE,
            {},
            {"alive_peer_count": 1, "os_heartbeat_absent_seconds": 600},
        )
        assert not result.passed
        assert "T1" in result.reason or "peer" in result.reason.lower()

    def test_power_cycle_requires_os_heartbeat_absent(self):
        result = check_preconditions(
            ActionType.POWER_CYCLE,
            {},
            {"alive_peer_count": 3, "os_heartbeat_absent_seconds": 60},
        )
        assert not result.passed
        assert "heartbeat" in result.reason.lower()

    def test_power_cycle_passes_all_conditions(self):
        result = check_preconditions(
            ActionType.POWER_CYCLE,
            {},
            {"alive_peer_count": 3, "os_heartbeat_absent_seconds": 600},
        )
        assert result.passed

    def test_power_cap_requires_active_event(self):
        result = check_preconditions(
            ActionType.POWER_CAP_ADJUST,
            {"thermal_event_active": False, "power_event_active": False},
            {},
        )
        assert not result.passed

    def test_power_cap_checks_policy_range(self):
        result = check_preconditions(
            ActionType.POWER_CAP_ADJUST,
            {
                "thermal_event_active": True,
                "power_cap_target_watts": 500,
                "power_cap_policy_min_watts": 200,
                "power_cap_policy_max_watts": 400,
            },
            {},
        )
        assert not result.passed
        assert "outside policy" in result.reason.lower()


class TestActionRisk:
    def test_r1_actions_are_none_or_low(self):
        assert ACTION_RISK[ActionType.IDENTIFY_LED] == "none"
        assert ACTION_RISK[ActionType.FAN_RESET] == "low"

    def test_r3a_actions_have_correct_risk(self):
        assert ACTION_RISK[ActionType.SEL_CLEAR] == "low"
        assert ACTION_RISK[ActionType.BMC_RESET] == "low"
        assert ACTION_RISK[ActionType.POWER_CYCLE] == "medium"
        assert ACTION_RISK[ActionType.POWER_CAP_ADJUST] == "medium"


# ===========================================================================
# P3: Blast Radius
# ===========================================================================


class TestBlastRadius:
    def test_unlimited_actions_always_allowed(self):
        limiter = BlastRadiusLimiter()
        now = 1000.0
        for _ in range(100):
            assert limiter.allows(ActionType.IDENTIFY_LED, now)
            limiter.record(ActionType.IDENTIFY_LED, now)

    def test_power_cycle_limited_to_one(self):
        limiter = BlastRadiusLimiter()
        now = 1000.0
        assert limiter.allows(ActionType.POWER_CYCLE, now)
        limiter.record(ActionType.POWER_CYCLE, now)
        assert not limiter.allows(ActionType.POWER_CYCLE, now + 60)

    def test_power_cycle_allowed_after_window(self):
        limiter = BlastRadiusLimiter()
        now = 1000.0
        limiter.record(ActionType.POWER_CYCLE, now)
        # Window is 1800s for power cycle
        assert limiter.allows(ActionType.POWER_CYCLE, now + 1801)

    def test_cooldown_enforced(self):
        limiter = BlastRadiusLimiter()
        now = 1000.0
        limiter.record(ActionType.FAN_RESET, now)
        # Cooldown is 300s for fan reset
        assert not limiter.allows(ActionType.FAN_RESET, now + 100)
        assert limiter.allows(ActionType.FAN_RESET, now + 301)

    def test_bmc_reset_max_one_per_4h(self):
        limiter = BlastRadiusLimiter()
        now = 1000.0
        limiter.record(ActionType.BMC_RESET, now)
        assert not limiter.allows(ActionType.BMC_RESET, now + 3600)
        assert limiter.allows(ActionType.BMC_RESET, now + 14401)

    def test_reset_clears_history(self):
        limiter = BlastRadiusLimiter()
        limiter.record(ActionType.POWER_CYCLE, 1000.0)
        assert not limiter.allows(ActionType.POWER_CYCLE, 1001.0)
        limiter.reset(ActionType.POWER_CYCLE)
        assert limiter.allows(ActionType.POWER_CYCLE, 1001.0)


# ===========================================================================
# P3: Verification + Outcome
# ===========================================================================


class TestVerification:
    def test_sel_clear_success(self):
        result = evaluate_verification(
            ActionType.SEL_CLEAR, {"sel_entry_count": 0}
        )
        assert result == OutcomeStatus.SUCCESS

    def test_sel_clear_failure(self):
        result = evaluate_verification(
            ActionType.SEL_CLEAR, {"sel_entry_count": 42}
        )
        assert result == OutcomeStatus.FAILURE

    def test_bmc_reset_success(self):
        result = evaluate_verification(
            ActionType.BMC_RESET, {"bmc_responsive": True}
        )
        assert result == OutcomeStatus.SUCCESS

    def test_missing_state_is_failure(self):
        result = evaluate_verification(ActionType.BMC_RESET, {})
        assert result == OutcomeStatus.FAILURE

    def test_verification_windows_defined_for_all_r3a_actions(self):
        for action_type in [ActionType.SEL_CLEAR, ActionType.BMC_RESET,
                           ActionType.POWER_CYCLE, ActionType.POWER_CAP_ADJUST]:
            assert action_type in VERIFICATION_WINDOWS


class TestActionOutcome:
    def test_outcome_dataclass(self):
        outcome = ActionOutcome(
            action_id="test-1",
            action_type=ActionType.SEL_CLEAR,
            outcome=OutcomeStatus.SUCCESS,
            fault_resolved=True,
        )
        assert outcome.action_id == "test-1"
        assert outcome.outcome == OutcomeStatus.SUCCESS
        assert outcome.fault_resolved is True
        assert outcome.pre_state == {}
        assert outcome.side_effects == []


# ===========================================================================
# P7: OS Signals
# ===========================================================================


class TestOSSignalCollector:
    def test_register_and_collect(self):
        collector = OSSignalCollector()

        class FakeSource:
            source_type = SignalSourceType.SYSLOG
            def collect(self):
                return [OSEvent(
                    source=SignalSourceType.SYSLOG, timestamp=time.time(),
                    severity="error", category="mce", message="MCE error",
                    raw_line="mce: error", component_hint="cpu",
                )]
            def reset(self):
                pass

        collector.register(FakeSource())
        assert "syslog" in collector.active_sources
        events = collector.collect_all()
        assert len(events) == 1
        assert events[0].category == "mce"

    def test_empty_collector_returns_empty(self):
        collector = OSSignalCollector()
        assert collector.collect_all() == []


class TestSyslogParsing:
    def test_mce_detected(self):
        source = SyslogSource(log_path="/nonexistent")
        event = source._parse_line("Aug 23 12:00:00 server1 kernel: mce: Bank 4 error")
        assert event is not None
        assert event.category == "mce"
        assert event.severity == "error"
        assert event.component_hint == "cpu"

    def test_pcie_aer_detected(self):
        source = SyslogSource(log_path="/nonexistent")
        event = source._parse_line(
            "Aug 23 12:00:00 server1 kernel: pcieport 0000:3b:00.0: AER: Corrected error"
        )
        assert event is not None
        assert event.category == "pcie_aer"
        assert "pci:0000:3b:00.0" in event.device_path

    def test_disk_io_error_detected(self):
        source = SyslogSource(log_path="/nonexistent")
        event = source._parse_line(
            "Aug 23 12:00:00 server1 kernel: sd 0:0:0:0: [sda] I/O error"
        )
        assert event is not None
        assert event.category == "disk_io"
        assert "sda" in event.device_path

    def test_nvme_error_detected(self):
        source = SyslogSource(log_path="/nonexistent")
        event = source._parse_line(
            "Aug 23 12:00:00 server1 kernel: nvme0: I/O error on queue 1"
        )
        assert event is not None
        assert event.category == "nvme"

    def test_non_hardware_line_ignored(self):
        source = SyslogSource(log_path="/nonexistent")
        event = source._parse_line(
            "Aug 23 12:00:00 server1 sshd[1234]: Accepted publickey for user"
        )
        assert event is None

    def test_edac_memory_error(self):
        source = SyslogSource(log_path="/nonexistent")
        event = source._parse_line(
            "Aug 23 12:00:00 server1 kernel: EDAC MC0: 1 CE error on DIMM0"
        )
        assert event is not None
        assert event.category == "mce"
        assert event.component_hint == "memory"

    def test_thermal_warning(self):
        source = SyslogSource(log_path="/nonexistent")
        event = source._parse_line(
            "Aug 23 12:00:00 server1 kernel: CPU0 Package temperature above threshold"
        )
        assert event is not None
        assert event.category == "thermal"


class TestSyslogCollection:
    def test_reads_from_file(self, tmp_path):
        log = tmp_path / "test.log"
        log.write_text(
            "Aug 23 12:00:00 server1 kernel: mce: Bank 4 error\n"
            "Aug 23 12:00:01 server1 sshd: normal login\n"
            "Aug 23 12:00:02 server1 kernel: sd 0:0:0:0: [sdb] I/O error\n"
        )
        source = SyslogSource(log_path=str(log))
        events = source.collect()
        assert len(events) == 2  # mce + disk_io
        assert events[0].category == "mce"
        assert events[1].category == "disk_io"

    def test_incremental_reading(self, tmp_path):
        log = tmp_path / "test.log"
        log.write_text("Aug 23 12:00:00 server1 kernel: mce: Bank 4 error\n")
        source = SyslogSource(log_path=str(log))
        events1 = source.collect()
        assert len(events1) == 1

        # Append more data
        with open(log, "a") as f:
            f.write("Aug 23 12:00:05 server1 kernel: sd 0:0:0:0: [sda] I/O error\n")
        events2 = source.collect()
        assert len(events2) == 1  # only the new line
        assert events2[0].category == "disk_io"


# ===========================================================================
# P7: Diagnosis Model
# ===========================================================================


class TestDiagnosisModel:
    def test_create_diagnosis(self):
        d = Diagnosis(
            device_id="server-1",
            component="DIMM.Socket.A1",
            summary="Memory ECC errors rising",
            tier=TierLevel.T1,
        )
        assert d.id  # auto-generated
        assert d.device_id == "server-1"
        assert d.component == "DIMM.Socket.A1"

    def test_add_evidence(self):
        d = Diagnosis()
        d.add_evidence("redfish", "memory:DIMM.A1", {"ecc_count": 42})
        d.add_evidence("os-signal", "dmesg:edac", "EDAC CE error on DIMM0")
        assert len(d.evidence) == 2
        assert d.evidence[0].source == "redfish"
        assert d.evidence[1].source == "os-signal"

    def test_confidence_dimensions_independent(self):
        d = Diagnosis()
        d.confidence = [
            ConfidenceDimension(name="baseline", value=0.9),
            ConfidenceDimension(name="skill_match", value=1.0),
        ]
        assert d.overall_confidence == 0.9  # min of all dimensions

    def test_overall_confidence_zero_when_empty(self):
        d = Diagnosis()
        assert d.overall_confidence == 0.0

    def test_reasoning_path(self):
        d = Diagnosis()
        d.add_reasoning_step("Skill 'memory-ecc' matched (100%)")
        d.add_reasoning_step("Baseline confidence: 0.92 (1200 samples)")
        d.add_reasoning_step("Corroborated by OS syslog EDAC event")
        assert len(d.reasoning_path) == 3

    def test_to_dict_serializable(self):
        d = Diagnosis(
            device_id="server-1",
            component="Fan1A",
            summary="Fan degrading",
            tier=TierLevel.T2,
            trajectory="degrading: RPM declining 5% per week",
        )
        d.confidence = [
            ConfidenceDimension(name="baseline", value=0.85, detail="720 samples"),
        ]
        d.add_evidence("redfish", "fan:Fan1A", 4200)
        result = d.to_dict()
        assert result["device_id"] == "server-1"
        assert result["tier"] == "t2"
        assert result["evidence_count"] == 1
        assert result["confidence"][0]["name"] == "baseline"

    def test_mixed_evidence_sources(self):
        """The 'hardware cause -> OS symptom' correlation capability."""
        d = Diagnosis(
            device_id="server-1",
            component="Disk.Bay.2",
            summary="Drive failing, mapped to /dev/sdb (/data/postgres)",
        )
        d.add_evidence("redfish", "disk:Bay2", {"smart_status": "PredictiveFailure"})
        d.add_evidence("os-signal", "syslog:disk_io", "sd 0:0:1:0: [sdb] I/O error")
        d.confidence = [
            ConfidenceDimension(name="skill_match", value=1.0),
            ConfidenceDimension(name="baseline", value=0.95),
        ]
        d.add_reasoning_step("Redfish SMART PredictiveFailure on Bay 2")
        d.add_reasoning_step("OS syslog shows I/O errors on /dev/sdb")
        d.add_reasoning_step("Serial match: Bay 2 = /dev/sdb = /data/postgres")

        assert len(d.evidence) == 2
        assert d.evidence[0].source == "redfish"
        assert d.evidence[1].source == "os-signal"
        assert d.overall_confidence == 0.95
