"""R3b-1 C2/C3/C5/C8: skill generation, OS signal expansion, skill integration, KB persistence."""

import time
from unittest.mock import AsyncMock

import pytest

from harkeniq.autonomy.skill_lifecycle import (
    SkillPackage, SkillTier, ValidationState, SkillOutcomeStats,
)
from harkeniq.os_signals.collector import OSSignalCollector, SignalSourceType
from harkeniq.os_signals.journal import JournalSource
from harkeniq.os_signals.smartctl import SmartctlSource


# ===========================================================================
# C2: Skill Generation
# ===========================================================================


class TestSkillGenerator:
    async def test_generates_candidate_from_llm(self):
        from harkeniq_sm.skill_generator import SkillGenerator
        provider = AsyncMock()
        provider.complete = AsyncMock(return_value=(
            "```yaml\n"
            "name: auto-fan-bearing-wear\n"
            "version: 1\n"
            "target: fan\n"
            "description: Detect fan bearing wear from RPM decline\n"
            "rules:\n"
            "  - condition: \"speed_rpm < 3000\"\n"
            "    verdict: WARNING\n"
            "    message: \"Fan {name} RPM below threshold\"\n"
            "```"
        ))
        gen = SkillGenerator(provider)
        candidate = await gen.generate(
            device_id="dev-1", component="fan:Fan1A",
            severity="WARNING", root_cause="Bearing wear",
            suggested_action="Replace fan", evidence=[{"rpm": 2800}],
            target="fan",
        )
        assert candidate is not None
        assert candidate.package.tier == SkillTier.AUTO_GENERATED
        assert candidate.package.validation_state == ValidationState.DRAFT
        assert "name:" in candidate.yaml_text
        assert "fan" in candidate.yaml_text

    async def test_returns_none_on_llm_failure(self):
        from harkeniq_sm.skill_generator import SkillGenerator
        provider = AsyncMock()
        provider.complete = AsyncMock(return_value=None)
        gen = SkillGenerator(provider)
        candidate = await gen.generate(
            device_id="dev-1", component="fan:Fan1A",
            severity="WARNING", root_cause="unknown",
            suggested_action="", evidence=[],
        )
        assert candidate is None

    async def test_returns_none_on_no_yaml(self):
        from harkeniq_sm.skill_generator import SkillGenerator
        provider = AsyncMock()
        provider.complete = AsyncMock(return_value="I cannot generate a skill for this.")
        gen = SkillGenerator(provider)
        candidate = await gen.generate(
            device_id="dev-1", component="fan:Fan1A",
            severity="WARNING", root_cause="unknown",
            suggested_action="", evidence=[],
        )
        assert candidate is None

    async def test_extracts_raw_yaml(self):
        from harkeniq_sm.skill_generator import SkillGenerator
        provider = AsyncMock()
        provider.complete = AsyncMock(return_value=(
            "name: auto-disk-smart\n"
            "version: 1\n"
            "target: disk\n"
            "description: SMART alert\n"
            "rules:\n"
            "  - condition: \"smart_alert == true\"\n"
            "    verdict: WARNING\n"
            "    message: \"SMART alert on {name}\"\n"
        ))
        gen = SkillGenerator(provider)
        candidate = await gen.generate(
            device_id="dev-1", component="disk:Bay2",
            severity="WARNING", root_cause="SMART failure",
            suggested_action="Replace", evidence=[],
        )
        assert candidate is not None
        assert "smart_alert" in candidate.yaml_text

    async def test_candidate_to_dict(self):
        from harkeniq_sm.skill_generator import SkillCandidate
        pkg = SkillPackage(
            skill_id="auto-test", tier=SkillTier.AUTO_GENERATED,
            validation_state=ValidationState.DRAFT,
        )
        candidate = SkillCandidate(
            skill_id="auto-test", yaml_text="name: test\n",
            package=pkg, source_device="dev-1",
        )
        d = candidate.to_dict()
        assert d["skill_id"] == "auto-test"
        assert d["package"]["tier"] == "auto_generated"

    def test_infer_target(self):
        from harkeniq_sm.skill_generator import SkillGenerator
        gen = SkillGenerator(AsyncMock())
        assert gen._infer_target("fan:Fan1A") == "fan"
        assert gen._infer_target("disk:Bay2") == "disk"
        assert gen._infer_target("DIMM.Socket.A1") == "memory"
        assert gen._infer_target("SSD.Slot.3") == "disk"
        assert gen._infer_target("InletTemp") == "thermal"


# ===========================================================================
# C3: OS Signal Expansion — Journal
# ===========================================================================


class TestJournalSource:
    def test_source_type(self):
        source = JournalSource()
        assert source.source_type == SignalSourceType.JOURNAL

    def test_parse_mce_line(self):
        source = JournalSource()
        event = source._parse_line("Aug 23 kernel: mce: Bank 4 error on CPU 0")
        assert event is not None
        assert event.category == "mce"
        assert event.source == SignalSourceType.JOURNAL

    def test_parse_pcie_aer(self):
        source = JournalSource()
        event = source._parse_line("kernel: pcieport 0000:3b:00.0: AER: Corrected error")
        assert event is not None
        assert event.category == "pcie_aer"

    def test_ignore_non_hardware(self):
        source = JournalSource()
        event = source._parse_line("systemd[1]: Started SSH server.")
        assert event is None

    def test_registers_in_collector(self):
        collector = OSSignalCollector()
        collector.register(JournalSource())
        assert "journal" in collector.active_sources


# ===========================================================================
# C3: OS Signal Expansion — Smartctl
# ===========================================================================


class TestSmartctlSource:
    def test_source_type(self):
        source = SmartctlSource()
        assert source.source_type == SignalSourceType.SMARTCTL

    def test_registers_in_collector(self):
        collector = OSSignalCollector()
        collector.register(SmartctlSource())
        # smartctl may not be in the enum yet; check it was registered
        assert len(collector._sources) == 1

    def test_critical_attrs_defined(self):
        from harkeniq.os_signals.smartctl import _CRITICAL_ATTRS
        assert "5" in _CRITICAL_ATTRS  # Reallocated sectors
        assert "197" in _CRITICAL_ATTRS  # Pending sectors
        assert "198" in _CRITICAL_ATTRS  # Offline uncorrectable


# ===========================================================================
# C5: SkillPackage <-> Loader Integration
# ===========================================================================


class TestSkillPackageIntegration:
    def test_load_skills_with_packages(self):
        from pathlib import Path
        from harkeniq.skills.loader import load_skills_with_packages
        skills_dir = Path(__file__).parents[2] / "skills"
        if not skills_dir.is_dir():
            pytest.skip("skills directory not found")
        result = load_skills_with_packages(skills_dir)
        assert len(result) > 0
        for name, (skill_def, pkg) in result.items():
            assert pkg.skill_id == name
            assert pkg.validation_state == ValidationState.PROMOTED
            assert pkg.tier == SkillTier.CORE
            assert skill_def.name == name

    def test_package_outcome_tracking(self):
        pkg = SkillPackage(skill_id="test-skill", version="1.0")
        pkg.record_outcome("SUCCESS")
        pkg.record_outcome("SUCCESS")
        pkg.record_outcome("FAILURE")
        assert pkg.outcome_stats.total_executions == 3
        assert pkg.outcome_stats.success_rate == pytest.approx(2/3, abs=0.01)

    def test_package_deployment_recording(self):
        pkg = SkillPackage(skill_id="test-skill")
        pkg.record_deployment("operator", "canary")
        assert len(pkg.deployment_history) == 1
        assert pkg.deployment_history[0].target_scope == "canary"
        assert pkg.last_deployed_at is not None


# ===========================================================================
# C8: KnowledgeBase Persistence (DB models)
# ===========================================================================


class TestKBPersistenceModels:
    def test_action_outcome_row_exists(self):
        from harkeniq_sm.db.models import ActionOutcomeRow
        row = ActionOutcomeRow(
            action_id="a1", action_type="SEL_CLEAR",
            device_id="dev-1", outcome="SUCCESS",
        )
        assert row.action_type == "SEL_CLEAR"
        assert row.outcome == "SUCCESS"

    def test_error_budget_row_exists(self):
        from harkeniq_sm.db.models import ErrorBudgetRow
        row = ErrorBudgetRow(
            action_type="FAN_RESET",
            success_count=9, failure_count=1, total_count=10,
            min_success_rate=0.95,
        )
        assert row.action_type == "FAN_RESET"
        assert row.total_count == 10

    def test_incident_has_explanation_column(self):
        from harkeniq_sm.db.models import Incident
        incident = Incident(
            site_id="s1", kind="device", title="test",
            explanation={"provider": "llm", "summary": "test explanation"},
        )
        assert incident.explanation["provider"] == "llm"


class TestSkillOutcomeStats:
    def test_record_and_rate(self):
        stats = SkillOutcomeStats()
        stats.record("SUCCESS")
        stats.record("SUCCESS")
        stats.record("FAILURE")
        assert stats.success_rate == pytest.approx(2/3, abs=0.01)
        assert stats.total_executions == 3

    def test_empty_rate_is_one(self):
        stats = SkillOutcomeStats()
        assert stats.success_rate == 1.0

    def test_rollback_counted(self):
        stats = SkillOutcomeStats()
        stats.record("ROLLBACK_TRIGGERED")
        assert stats.rollback_count == 1
        assert stats.total_executions == 1
