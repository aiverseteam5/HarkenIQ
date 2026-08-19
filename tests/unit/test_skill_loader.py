"""Unit tests for the skill YAML loader (Doc 07 §2/§8, Doc 12 §2.1)."""

import pytest

from harkeniq.errors import SkillParseError, SkillValidationError
from harkeniq.models import ActionType, VerdictSeverity
from harkeniq.skills.loader import (
    load_skill_file,
    load_skills,
    parse_skill,
)


def make_skill(**overrides):
    """Minimal valid skill dict; override any field for error tests."""
    data = {
        "name": "test-skill",
        "version": 1,
        "target": "fan",
        "description": "test",
        "rules": [
            {
                "condition": "health == 'Critical'",
                "verdict": "CRITICAL",
                "message": "Fan {name} critical",
            }
        ],
    }
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# Valid skills
# ---------------------------------------------------------------------------


class TestValidSkills:
    def test_minimal_skill(self):
        skill = parse_skill(make_skill())
        assert skill.name == "test-skill"
        assert skill.target == "fan"
        assert len(skill.rules) == 1
        assert skill.rules[0].verdict == VerdictSeverity.CRITICAL
        assert skill.default_verdict == VerdictSeverity.HEALTHY

    def test_rule_with_debounce_and_action(self):
        skill = parse_skill(make_skill(rules=[{
            "condition": "speed_rpm < threshold_low_critical",
            "verdict": "CRITICAL",
            "message": "Fan {name} below threshold",
            "debounce": {"count": 2, "window": 3},
            "action": {"type": "IDENTIFY_LED", "params": {"target": "chassis"}},
        }]))
        rule = skill.rules[0]
        assert rule.debounce.count == 2
        assert rule.debounce.window == 3
        assert rule.action.type == ActionType.IDENTIFY_LED
        assert rule.action.params == {"target": "chassis"}

    def test_trending_rule(self):
        skill = parse_skill(make_skill(trending=[{
            "field": "speed_rpm",
            "direction": "declining",
            "verdict": "TRENDING",
            "message": "Fan {name} declining",
            "threshold_field": "threshold_low_critical",
        }]))
        t = skill.trending[0]
        assert t.field == "speed_rpm"
        assert t.direction == "declining"
        assert t.verdict == VerdictSeverity.TRENDING
        assert t.threshold_field == "threshold_low_critical"

    def test_trending_numeric_threshold_constant(self):
        # disk-health projects SSD wear toward 0 (Doc 07 §7.2)
        skill = parse_skill(make_skill(
            target="disk",
            rules=[{
                "condition": "health == 'Critical'",
                "verdict": "CRITICAL",
                "message": "Disk {name} critical",
            }],
            trending=[{
                "field": "life_left_pct",
                "direction": "declining",
                "verdict": "TRENDING",
                "message": "SSD {name} wearing out",
                "threshold_field": 0,
            }],
        ))
        assert skill.trending[0].threshold_field == "0"

    def test_baseline_fields_valid_for_all_targets(self):
        skill = parse_skill(make_skill(rules=[{
            "condition": "deviation > 3 AND baseline_stddev > 0",
            "verdict": "WARNING",
            "message": "Fan {name} deviating",
        }]))
        assert len(skill.rules) == 1

    def test_default_verdict_override(self):
        skill = parse_skill(make_skill(default_verdict="UNKNOWN"))
        assert skill.default_verdict == VerdictSeverity.UNKNOWN


# ---------------------------------------------------------------------------
# Validation errors (Doc 12 §2.1 error matrix)
# ---------------------------------------------------------------------------


class TestValidationErrors:
    def test_not_a_mapping(self):
        with pytest.raises(SkillValidationError, match="mapping"):
            parse_skill(["not", "a", "dict"])

    def test_missing_name(self):
        data = make_skill()
        del data["name"]
        with pytest.raises(SkillValidationError, match="name is required"):
            parse_skill(data)

    def test_unknown_top_level_key(self):
        with pytest.raises(SkillValidationError, match="unknown fields"):
            parse_skill(make_skill(extra_key="nope"))

    def test_bad_version(self):
        with pytest.raises(SkillValidationError, match="version"):
            parse_skill(make_skill(version=2))

    def test_missing_version(self):
        data = make_skill()
        del data["version"]
        with pytest.raises(SkillValidationError, match="version"):
            parse_skill(data)

    def test_unknown_target(self):
        with pytest.raises(SkillValidationError, match="unknown target"):
            parse_skill(make_skill(target="gpu"))

    def test_empty_rules(self):
        with pytest.raises(SkillValidationError, match="at least one rule"):
            parse_skill(make_skill(rules=[]))

    def test_missing_rules(self):
        data = make_skill()
        del data["rules"]
        with pytest.raises(SkillValidationError, match="at least one rule"):
            parse_skill(data)

    def test_rule_missing_condition(self):
        with pytest.raises(SkillValidationError, match="condition is required"):
            parse_skill(make_skill(rules=[{"verdict": "CRITICAL", "message": "x"}]))

    def test_rule_bad_condition_syntax(self):
        with pytest.raises(SkillParseError):
            parse_skill(make_skill(rules=[{
                "condition": "health ==",
                "verdict": "CRITICAL",
                "message": "x",
            }]))

    def test_unknown_field_for_target(self):
        # life_left_pct is a disk field, not a fan field
        with pytest.raises(SkillValidationError, match="unknown field 'life_left_pct'"):
            parse_skill(make_skill(rules=[{
                "condition": "life_left_pct < 10",
                "verdict": "WARNING",
                "message": "x",
            }]))

    def test_unknown_field_in_field_ref(self):
        with pytest.raises(SkillValidationError, match="unknown field"):
            parse_skill(make_skill(rules=[{
                "condition": "speed_rpm < bogus_threshold",
                "verdict": "WARNING",
                "message": "x",
            }]))

    def test_unknown_verdict(self):
        with pytest.raises(SkillValidationError, match="unknown verdict"):
            parse_skill(make_skill(rules=[{
                "condition": "health == 'Critical'",
                "verdict": "FATAL",
                "message": "x",
            }]))

    def test_rule_missing_message(self):
        with pytest.raises(SkillValidationError, match="message is required"):
            parse_skill(make_skill(rules=[{
                "condition": "health == 'Critical'",
                "verdict": "CRITICAL",
            }]))

    def test_rule_unknown_key(self):
        with pytest.raises(SkillValidationError, match="unknown fields"):
            parse_skill(make_skill(rules=[{
                "condition": "health == 'Critical'",
                "verdict": "CRITICAL",
                "message": "x",
                "severity": "high",
            }]))

    def test_bad_action_type(self):
        with pytest.raises(SkillValidationError, match="unknown action type"):
            parse_skill(make_skill(rules=[{
                "condition": "health == 'Critical'",
                "verdict": "CRITICAL",
                "message": "x",
                "action": {"type": "REBOOT_SERVER"},
            }]))

    def test_debounce_count_exceeds_window(self):
        with pytest.raises(SkillValidationError, match="exceeds window"):
            parse_skill(make_skill(rules=[{
                "condition": "health == 'Critical'",
                "verdict": "CRITICAL",
                "message": "x",
                "debounce": {"count": 5, "window": 3},
            }]))

    def test_debounce_non_positive(self):
        with pytest.raises(SkillValidationError, match="positive"):
            parse_skill(make_skill(rules=[{
                "condition": "health == 'Critical'",
                "verdict": "CRITICAL",
                "message": "x",
                "debounce": {"count": 0, "window": 3},
            }]))

    def test_debounce_missing_window(self):
        with pytest.raises(SkillValidationError, match="count and window"):
            parse_skill(make_skill(rules=[{
                "condition": "health == 'Critical'",
                "verdict": "CRITICAL",
                "message": "x",
                "debounce": {"count": 2},
            }]))

    def test_trending_bad_direction(self):
        with pytest.raises(SkillValidationError, match="declining.*rising"):
            parse_skill(make_skill(trending=[{
                "field": "speed_rpm",
                "direction": "sideways",
                "message": "x",
            }]))

    def test_trending_verdict_must_be_trending(self):
        with pytest.raises(SkillValidationError, match="must be TRENDING"):
            parse_skill(make_skill(trending=[{
                "field": "speed_rpm",
                "direction": "declining",
                "verdict": "WARNING",
                "message": "x",
            }]))

    def test_trending_unknown_field(self):
        with pytest.raises(SkillValidationError, match="unknown field"):
            parse_skill(make_skill(trending=[{
                "field": "life_left_pct",
                "direction": "declining",
                "message": "x",
            }]))

    def test_trending_bad_threshold_field(self):
        with pytest.raises(SkillValidationError, match="threshold_field"):
            parse_skill(make_skill(trending=[{
                "field": "speed_rpm",
                "direction": "declining",
                "message": "x",
                "threshold_field": "not_a_field",
            }]))


# ---------------------------------------------------------------------------
# File / directory loading
# ---------------------------------------------------------------------------


class TestFileLoading:
    def test_load_skill_file(self, tmp_path):
        f = tmp_path / "s.yaml"
        f.write_text(
            "name: file-skill\nversion: 1\ntarget: fan\n"
            "rules:\n  - condition: \"health == 'Critical'\"\n"
            "    verdict: CRITICAL\n    message: bad\n"
        )
        skill = load_skill_file(f)
        assert skill.name == "file-skill"

    def test_invalid_yaml(self, tmp_path):
        f = tmp_path / "bad.yaml"
        f.write_text("name: [unclosed\n")
        with pytest.raises(SkillValidationError, match="invalid YAML"):
            load_skill_file(f)

    def test_load_skills_directory_missing(self, tmp_path):
        with pytest.raises(SkillValidationError, match="does not exist"):
            load_skills(tmp_path / "nope")

    def test_duplicate_skill_names(self, tmp_path):
        content = (
            "name: dupe\nversion: 1\ntarget: fan\n"
            "rules:\n  - condition: \"health == 'Critical'\"\n"
            "    verdict: CRITICAL\n    message: bad\n"
        )
        (tmp_path / "a.yaml").write_text(content)
        (tmp_path / "b.yaml").write_text(content)
        with pytest.raises(SkillValidationError, match="duplicate"):
            load_skills(tmp_path)

    def test_load_default_skills(self):
        """The 5 bundled skills in skills/ must all load (Doc 07 §7)."""
        skills = load_skills("skills")
        assert set(skills) == {
            "fan-health", "disk-health", "memory-health",
            "psu-health", "thermal-health",
        }
        targets = {s.target for s in skills.values()}
        assert targets == {"fan", "disk", "memory", "psu", "thermal"}
        for skill in skills.values():
            assert skill.rules
            assert skill.default_verdict == VerdictSeverity.HEALTHY
