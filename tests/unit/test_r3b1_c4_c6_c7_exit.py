"""R3b-1 C4/C6/C7 + Exit Gate: app mapping, skill validation, distribution."""

import time

import pytest

from harkeniq.os_signals.device_map import DeviceMapping, FullDeviceMapping, HardwareDeviceMapper
from harkeniq.autonomy.skill_receiver import SkillReceiver
from harkeniq.autonomy.skill_lifecycle import SkillPackage, SkillTier, ValidationState
from harkeniq_sm.skill_validation import SkillValidator, ValidationResult
from harkeniq_sm.skill_generator import SkillGenerator, SkillCandidate


# ===========================================================================
# C4: Full App Mapping
# ===========================================================================


class TestFullDeviceMapping:
    def test_full_mapping_dataclass(self):
        m = FullDeviceMapping(
            redfish_id="Disk.Bay.2",
            serial_number="ABC123",
            os_device="/dev/sdb",
            mount_point="/data/postgres",
            filesystem="ext4",
            processes=[{"pid": 1234, "comm": "postgres"}],
            services=["postgresql.service"],
        )
        assert m.os_device == "/dev/sdb"
        assert m.services == ["postgresql.service"]

    def test_impact_summary(self):
        m = FullDeviceMapping(
            redfish_id="Disk.Bay.2", serial_number="ABC123",
            os_device="/dev/sdb", mount_point="/data/postgres",
            filesystem="ext4",
            services=["postgresql.service"],
        )
        summary = m.impact_summary()
        assert "Disk.Bay.2" in summary
        assert "/dev/sdb" in summary
        assert "/data/postgres" in summary
        assert "postgresql.service" in summary

    def test_impact_summary_no_services(self):
        m = FullDeviceMapping(
            redfish_id="Disk.Bay.3", serial_number="DEF456",
            os_device="/dev/sdc", mount_point="/var/log",
            filesystem="xfs",
            processes=[{"pid": 100, "comm": "rsyslogd"}],
        )
        summary = m.impact_summary()
        assert "rsyslogd" in summary

    def test_impact_summary_minimal(self):
        m = FullDeviceMapping(
            redfish_id="Disk.Bay.1", serial_number="GHI789",
            os_device="/dev/sda",
        )
        summary = m.impact_summary()
        assert "/dev/sda" in summary


# ===========================================================================
# C6: Skill Validation
# ===========================================================================

VALID_SKILL_YAML = """\
name: test-fan-alert
version: 1
target: fan
description: Test fan alert
rules:
  - condition: "health == 'Critical'"
    verdict: CRITICAL
    message: "Fan {name} critical"
"""

INVALID_SKILL_YAML = """\
name: bad-skill
version: 1
target: nonexistent_target
rules:
  - condition: "health == 'Critical'"
    verdict: CRITICAL
    message: "Bad"
"""

DANGEROUS_SKILL_YAML = """\
name: auto-power-cycle
version: 1
target: fan
description: Auto power cycle on fan failure
rules:
  - condition: "health == 'Critical'"
    verdict: CRITICAL
    message: "Fan failed, power cycling"
    action:
      type: POWER_CYCLE
      params:
        reason: "auto recovery"
"""


class TestSkillValidation:
    def test_valid_skill_passes_static(self):
        validator = SkillValidator()
        result = validator.validate_static(VALID_SKILL_YAML)
        assert result.passed
        assert result.stage == "static_analysis"
        assert len(result.errors) == 0

    def test_invalid_target_fails(self):
        validator = SkillValidator()
        result = validator.validate_static(INVALID_SKILL_YAML)
        assert not result.passed
        assert any("target" in e.lower() or "nonexistent" in e.lower() for e in result.errors)

    def test_invalid_yaml_fails(self):
        validator = SkillValidator()
        result = validator.validate_static("{{not: yaml: [")
        assert not result.passed
        assert any("yaml" in e.lower() for e in result.errors)

    def test_dangerous_action_warns(self):
        validator = SkillValidator()
        result = validator.validate_static(DANGEROUS_SKILL_YAML)
        assert result.passed  # passes but with warnings
        assert any("POWER_CYCLE" in w for w in result.warnings)

    def test_dry_run_with_matching_state(self):
        validator = SkillValidator()
        historical = [
            {"name": "Fan1A", "health": "Critical", "state": "Enabled",
             "speed_rpm": 0, "speed_pct": 0, "threshold_low_critical": 1000,
             "redundancy_health": "Degraded", "location": "Chassis"},
        ]
        result = validator.validate_dry_run(VALID_SKILL_YAML, historical)
        assert result.passed
        assert result.dry_run_matches >= 1

    def test_dry_run_no_matches_warns(self):
        validator = SkillValidator()
        historical = [
            {"name": "Fan1A", "health": "OK", "state": "Enabled",
             "speed_rpm": 7000, "speed_pct": 85, "threshold_low_critical": 1000,
             "redundancy_health": "OK", "location": "Chassis"},
        ]
        result = validator.validate_dry_run(VALID_SKILL_YAML, historical)
        assert result.passed
        assert result.dry_run_matches == 0
        assert any("matched 0" in w for w in result.warnings)

    def test_dry_run_empty_history_warns(self):
        validator = SkillValidator()
        result = validator.validate_dry_run(VALID_SKILL_YAML, [])
        assert result.passed
        assert any("No historical" in w for w in result.warnings)

    def test_validate_and_promote(self):
        validator = SkillValidator()
        pkg = SkillPackage(
            skill_id="test", tier=SkillTier.AUTO_GENERATED,
            validation_state=ValidationState.DRAFT,
        )
        result, updated = validator.validate_and_promote(VALID_SKILL_YAML, pkg)
        assert result.passed
        assert updated.validation_state == ValidationState.TESTED


# ===========================================================================
# C7: Skill Distribution — Agent Receiver
# ===========================================================================


class TestSkillReceiver:
    def test_receives_valid_skill(self, tmp_path):
        receiver = SkillReceiver(str(tmp_path / "skills"))
        accepted, reason = receiver.receive(
            skill_id="fan-health",
            version="1.0",
            yaml_content=VALID_SKILL_YAML,
            tier="core",
            validation_state="promoted",
        )
        assert accepted
        assert "installed" in reason
        assert (tmp_path / "skills" / "fan-health.yaml").exists()

    def test_rejects_draft_skills(self, tmp_path):
        receiver = SkillReceiver(str(tmp_path / "skills"))
        accepted, reason = receiver.receive(
            skill_id="draft-skill",
            version="0.1",
            yaml_content=VALID_SKILL_YAML,
            tier="auto_generated",
            validation_state="draft",
        )
        assert not accepted
        assert "DRAFT" in reason

    def test_rejects_invalid_yaml(self, tmp_path):
        receiver = SkillReceiver(str(tmp_path / "skills"))
        accepted, reason = receiver.receive(
            skill_id="bad-skill",
            version="1.0",
            yaml_content="not: valid: skill: yaml",
            tier="core",
            validation_state="tested",
        )
        assert not accepted
        assert "Validation" in reason or "failed" in reason.lower()

    def test_accepts_tested_skills(self, tmp_path):
        receiver = SkillReceiver(str(tmp_path / "skills"))
        accepted, _ = receiver.receive(
            skill_id="tested-skill",
            version="1.0",
            yaml_content=VALID_SKILL_YAML,
            tier="community",
            validation_state="tested",
        )
        assert accepted

    def test_list_installed(self, tmp_path):
        skills_dir = tmp_path / "skills"
        receiver = SkillReceiver(str(skills_dir))
        receiver.receive("skill-a", "1.0", VALID_SKILL_YAML, "core", "promoted")
        receiver.receive("skill-b", "1.0", VALID_SKILL_YAML, "core", "promoted")
        installed = receiver.list_installed()
        assert "skill-a" in installed
        assert "skill-b" in installed


# ===========================================================================
# R3b-1 EXIT GATE
# ===========================================================================


class TestR3b1ExitGate:
    """Verify all R3b-1 capabilities are implemented and work end-to-end."""

    def test_c1_llm_explain_exists(self):
        """C1: LLMReasoner plugs into ReasoningPipeline."""
        from harkeniq_sm.reasoning import LLMReasoner, ReasoningPipeline
        from harkeniq_sm.llm_provider import NullLLMProvider
        pipeline = ReasoningPipeline()
        pipeline.add_provider(LLMReasoner(NullLLMProvider()))
        assert len(pipeline._providers) == 1

    async def test_c2_skill_generation(self):
        """C2: SkillGenerator produces candidates from LLM output."""
        from unittest.mock import AsyncMock
        from harkeniq_sm.skill_generator import SkillGenerator
        provider = AsyncMock()
        provider.complete = AsyncMock(return_value=(
            "name: auto-test\nversion: 1\ntarget: fan\n"
            "description: test\nrules:\n"
            "  - condition: \"health == 'Critical'\"\n"
            "    verdict: CRITICAL\n    message: \"test\"\n"
        ))
        gen = SkillGenerator(provider)
        candidate = await gen.generate(
            "dev-1", "fan:Fan1A", "WARNING", "bearing wear", "replace", [],
        )
        assert candidate is not None
        assert candidate.package.tier == SkillTier.AUTO_GENERATED

    def test_c3_os_signal_expansion(self):
        """C3: Journal and Smartctl sources register in collector."""
        from harkeniq.os_signals.collector import OSSignalCollector
        from harkeniq.os_signals.journal import JournalSource
        from harkeniq.os_signals.smartctl import SmartctlSource
        collector = OSSignalCollector()
        collector.register(JournalSource())
        collector.register(SmartctlSource())
        assert len(collector._sources) == 2

    def test_c4_full_app_mapping(self):
        """C4: FullDeviceMapping with process + service info."""
        m = FullDeviceMapping(
            redfish_id="Disk.Bay.2", serial_number="S1",
            os_device="/dev/sdb", mount_point="/data",
            services=["postgresql.service"],
        )
        assert "postgresql.service" in m.impact_summary()

    def test_c5_skill_package_integration(self):
        """C5: load_skills_with_packages wraps skills with lifecycle."""
        from pathlib import Path
        from harkeniq.skills.loader import load_skills_with_packages
        skills_dir = Path(__file__).parents[2] / "skills"
        if not skills_dir.is_dir():
            pytest.skip("skills directory not found")
        result = load_skills_with_packages(skills_dir)
        assert len(result) > 0
        for _, (_, pkg) in result.items():
            assert pkg.validation_state == ValidationState.PROMOTED

    def test_c6_skill_validation(self):
        """C6: SkillValidator validates and promotes candidates."""
        validator = SkillValidator()
        pkg = SkillPackage(skill_id="t", validation_state=ValidationState.DRAFT)
        result, updated = validator.validate_and_promote(VALID_SKILL_YAML, pkg)
        assert result.passed
        assert updated.validation_state == ValidationState.TESTED

    def test_c7_skill_distribution(self, tmp_path):
        """C7: SkillReceiver installs pushed skills on agent."""
        receiver = SkillReceiver(str(tmp_path / "skills"))
        accepted, _ = receiver.receive(
            "distributed-skill", "1.0", VALID_SKILL_YAML, "core", "promoted",
        )
        assert accepted
        assert "distributed-skill" in receiver.list_installed()

    def test_c8_kb_persistence_models(self):
        """C8: DB models for outcome and error budget persistence."""
        from harkeniq_sm.db.models import ActionOutcomeRow, ErrorBudgetRow
        outcome = ActionOutcomeRow(
            action_id="a1", action_type="SEL_CLEAR",
            device_id="dev-1", outcome="SUCCESS",
        )
        budget = ErrorBudgetRow(
            action_type="SEL_CLEAR", success_count=10,
            failure_count=0, total_count=10,
        )
        assert outcome.outcome == "SUCCESS"
        assert budget.total_count == 10

    def test_end_to_end_skill_lifecycle(self, tmp_path):
        """Full flow: generate -> validate -> distribute -> install."""
        # 1. Candidate generated (simulated)
        yaml_text = VALID_SKILL_YAML
        pkg = SkillPackage(
            skill_id="auto-fan-alert",
            tier=SkillTier.AUTO_GENERATED,
            validation_state=ValidationState.DRAFT,
        )

        # 2. Validate
        validator = SkillValidator()
        result, pkg = validator.validate_and_promote(yaml_text, pkg)
        assert result.passed
        assert pkg.validation_state == ValidationState.TESTED

        # 3. Distribute to agent
        receiver = SkillReceiver(str(tmp_path / "skills"))
        accepted, _ = receiver.receive(
            pkg.skill_id, pkg.version, yaml_text,
            pkg.tier.value, pkg.validation_state.value,
        )
        assert accepted

        # 4. Agent can load the installed skill
        from harkeniq.skills.loader import load_skills
        skills = load_skills(tmp_path / "skills")
        assert "test-fan-alert" in skills  # name from YAML, not skill_id

    def test_deterministic_fallback_preserved(self):
        """Deterministic reasoning still works when LLM is disabled."""
        from harkeniq_sm.reasoning import (
            DeterministicReasoner, KnowledgeBaseReasoner,
            ReasoningContext, ReasoningPipeline,
        )
        pipeline = ReasoningPipeline()
        pipeline.add_provider(DeterministicReasoner())
        pipeline.add_provider(KnowledgeBaseReasoner())
        # No LLMReasoner added = LLM disabled

        context = ReasoningContext(
            device_id="dev-1", component="fan:Fan1A",
            severity="WARNING", evidence=[{"rpm": 4200}],
        )
        result = pipeline.analyze(context)
        assert result is not None
        assert result.provider == "deterministic"
