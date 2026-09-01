"""Unit tests for the skill evaluation engine (Doc 07 §4, Doc 12 §2.2)."""

from harkeniq.models import VerdictSeverity
from harkeniq.redfish.normalize import NormalizedFan
from harkeniq.skills.engine import (
    build_context,
    evaluate_skill,
    format_message,
    winning_rule,
)
from harkeniq.skills.loader import parse_skill


def fan_skill(rules=None, trending=None, default_verdict=None):
    data = {
        "name": "fan-test",
        "version": 1,
        "target": "fan",
        "rules": rules or [
            {
                "condition": "health == 'Critical'",
                "verdict": "CRITICAL",
                "message": "Fan {name} health critical",
            },
            {
                "condition": "speed_rpm < threshold_low_critical",
                "verdict": "CRITICAL",
                "message": "Fan {name} at {speed_rpm} RPM below {threshold_low_critical}",
                # A22.2: IDENTIFY_LED requires a target, and `parse_skill`
                # now refuses a skill that omits it -- the class could
                # never have executed without one.
                "action": {"type": "IDENTIFY_LED", "params": {"target": "{name}"}},
            },
            {
                "condition": "health == 'Warning'",
                "verdict": "WARNING",
                "message": "Fan {name} health warning",
            },
        ],
    }
    if trending:
        data["trending"] = trending
    if default_verdict:
        data["default_verdict"] = default_verdict
    return parse_skill(data)


def healthy_fan(**overrides):
    fields = dict(
        name="Fan1", speed_rpm=9200, speed_pct=32, health="OK",
        state="Enabled", threshold_low_critical=480, location="System Board Fan1A",
    )
    fields.update(overrides)
    return NormalizedFan(**fields)


class TestBuildContext:
    def test_from_dataclass(self):
        ctx = build_context(healthy_fan())
        assert ctx["name"] == "Fan1"
        assert ctx["speed_rpm"] == 9200

    def test_from_dict(self):
        ctx = build_context({"name": "x", "health": "OK"})
        assert ctx == {"name": "x", "health": "OK"}

    def test_baseline_merged(self):
        ctx = build_context(healthy_fan(), {"baseline_mean": 9100.0, "deviation": 0.5})
        assert ctx["baseline_mean"] == 9100.0
        assert ctx["deviation"] == 0.5

    def test_invalid_sensor_type(self):
        import pytest
        with pytest.raises(TypeError):
            build_context("not a sensor")


class TestFormatMessage:
    def test_substitution(self):
        assert format_message("Fan {name} at {speed_rpm} RPM",
                              {"name": "Fan1", "speed_rpm": 400}) == "Fan Fan1 at 400 RPM"

    def test_unknown_placeholder_left_intact(self):
        assert format_message("{name} {nope}", {"name": "x"}) == "x {nope}"


class TestEvaluateSkill:
    def test_no_match_default_healthy(self):
        verdict = evaluate_skill(fan_skill(), healthy_fan())
        assert verdict.severity == VerdictSeverity.HEALTHY
        assert verdict.sensor_id == "fan:Fan1"
        assert verdict.skill_name == "fan-test"
        assert verdict.evidence == []
        assert "Fan1" in verdict.message

    def test_single_rule_match(self):
        verdict = evaluate_skill(fan_skill(), healthy_fan(health="Warning"))
        assert verdict.severity == VerdictSeverity.WARNING
        assert verdict.message == "Fan Fan1 health warning"
        assert len(verdict.evidence) == 1
        assert verdict.evidence[0].rule_index == 2

    def test_multiple_rules_highest_severity_wins(self):
        # health Critical (rule 0, CRITICAL) + low rpm (rule 1, CRITICAL)
        # + not Warning: two CRITICAL matches, first wins the message
        verdict = evaluate_skill(
            fan_skill(), healthy_fan(health="Critical", speed_rpm=400)
        )
        assert verdict.severity == VerdictSeverity.CRITICAL
        assert verdict.message == "Fan Fan1 health critical"
        assert len(verdict.evidence) == 2
        assert [e.rule_index for e in verdict.evidence] == [0, 1]

    def test_warning_and_critical_critical_wins(self):
        skill = fan_skill(rules=[
            {"condition": "speed_rpm < 10000", "verdict": "WARNING",
             "message": "warn {name}"},
            {"condition": "speed_rpm < 500", "verdict": "CRITICAL",
             "message": "crit {name}"},
        ])
        verdict = evaluate_skill(skill, healthy_fan(speed_rpm=400))
        assert verdict.severity == VerdictSeverity.CRITICAL
        assert verdict.message == "crit Fan1"
        assert len(verdict.evidence) == 2

    def test_message_field_substitution(self):
        verdict = evaluate_skill(fan_skill(), healthy_fan(speed_rpm=400))
        assert verdict.message == "Fan Fan1 at 400 RPM below 480"

    def test_evidence_contents(self):
        baseline = {"baseline_mean": 9000.0, "baseline_confidence": 0.8}
        verdict = evaluate_skill(
            fan_skill(), healthy_fan(speed_rpm=400), baseline=baseline
        )
        ev = verdict.evidence[0]
        assert ev.sensor_id == "fan:Fan1"
        assert ev.skill_name == "fan-test"
        assert ev.condition == "speed_rpm < threshold_low_critical"
        assert ev.fields["speed_rpm"] == 400
        assert ev.fields["health"] == "OK"
        assert ev.baseline_confidence == 0.8

    def test_evidence_excludes_none_and_nested(self):
        verdict = evaluate_skill(fan_skill(), healthy_fan(speed_rpm=400, speed_pct=None))
        fields = verdict.evidence[0].fields
        assert "speed_pct" not in fields
        assert "oem_data" not in fields

    def test_default_verdict_unknown(self):
        verdict = evaluate_skill(
            fan_skill(default_verdict="UNKNOWN"), healthy_fan()
        )
        assert verdict.severity == VerdictSeverity.UNKNOWN

    def test_missing_data_never_triggers(self):
        # No threshold_low_critical → rule 1 cannot fire (Doc 07 §3.6)
        verdict = evaluate_skill(
            fan_skill(), healthy_fan(speed_rpm=100, threshold_low_critical=None)
        )
        assert verdict.severity == VerdictSeverity.HEALTHY

    def test_timestamp_passthrough(self):
        ts = "2026-01-01T00:00:00Z"
        verdict = evaluate_skill(fan_skill(), healthy_fan(), timestamp=ts)
        assert verdict.timestamp == ts

    def test_rule_exception_does_not_abort_evaluation(self, monkeypatch):
        skill = fan_skill()
        # Sabotage rule 0's AST so evaluate raises; rule 1 must still match
        skill.rules[0].parsed_ast = object()
        verdict = evaluate_skill(skill, healthy_fan(speed_rpm=400))
        assert verdict.severity == VerdictSeverity.CRITICAL
        assert [e.rule_index for e in verdict.evidence] == [1]


class TestWinningRule:
    def test_returns_rule_with_action(self):
        skill = fan_skill()
        verdict = evaluate_skill(skill, healthy_fan(speed_rpm=400))
        rule = winning_rule(skill, verdict)
        assert rule is skill.rules[1]
        assert rule.action is not None

    def test_first_matching_severity_wins(self):
        skill = fan_skill()
        verdict = evaluate_skill(skill, healthy_fan(health="Critical", speed_rpm=400))
        assert winning_rule(skill, verdict) is skill.rules[0]

    def test_no_evidence_returns_none(self):
        skill = fan_skill()
        verdict = evaluate_skill(skill, healthy_fan())
        assert winning_rule(skill, verdict) is None


class TestDefaultSkillsEndToEnd:
    """Bundled skills evaluated against realistic sensor readings."""

    def test_fan_health_critical_on_failed_fan(self):
        from harkeniq.skills.loader import load_skills
        skills = load_skills("skills")
        verdict = evaluate_skill(
            skills["fan-health"],
            healthy_fan(health="Critical", state="Enabled", speed_rpm=0),
        )
        assert verdict.severity == VerdictSeverity.CRITICAL

    def test_fan_health_healthy(self):
        from harkeniq.skills.loader import load_skills
        skills = load_skills("skills")
        verdict = evaluate_skill(skills["fan-health"], healthy_fan())
        assert verdict.severity == VerdictSeverity.HEALTHY

    def test_disk_health_smart_alert(self):
        from harkeniq.redfish.normalize import NormalizedDisk
        from harkeniq.skills.loader import load_skills
        skills = load_skills("skills")
        disk = NormalizedDisk(
            name="Disk0", serial="S1", media_type="SSD", protocol="SATA",
            health="OK", smart_alert=True, life_left_pct=80,
        )
        verdict = evaluate_skill(skills["disk-health"], disk)
        assert verdict.severity >= VerdictSeverity.WARNING
