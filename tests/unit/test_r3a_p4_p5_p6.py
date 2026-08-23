"""R3a P4/P5/P6: Autonomy budgets, stop switch, outcomes, error budget, suppression."""

import time

import pytest

from harkeniq.autonomy.budget import AgentBudgetEnforcer
from harkeniq.models import ActionType

# SM modules
from harkeniq_sm.autonomy import SMAutonomyEnforcer, SiteBudgetCounter
from harkeniq_sm.knowledge import ErrorBudgetState, KnowledgeBase, StoredOutcome
from harkeniq_sm.suppression import (
    CorrelationEvent,
    SuppressionEngine,
    SuppressionPolicy,
    STABILITY_PERIOD,
    HAIR_TRIGGER_WINDOW,
)


# ===========================================================================
# P4: Agent Budget Enforcement
# ===========================================================================


class TestAgentBudget:
    def test_allows_when_no_budget_set(self):
        enforcer = AgentBudgetEnforcer()
        assert enforcer.allows(ActionType.SEL_CLEAR)

    def test_allows_unlimited_budget(self):
        enforcer = AgentBudgetEnforcer()
        enforcer.update_from_lease({"SEL_CLEAR": -1}, stop_switch=False)
        assert enforcer.allows(ActionType.SEL_CLEAR)

    def test_allows_when_budget_remaining(self):
        enforcer = AgentBudgetEnforcer()
        enforcer.update_from_lease({"FAN_RESET": 5}, stop_switch=False)
        assert enforcer.allows(ActionType.FAN_RESET)

    def test_denies_when_budget_exhausted(self):
        enforcer = AgentBudgetEnforcer()
        enforcer.update_from_lease({"FAN_RESET": 2}, stop_switch=False)
        enforcer.consume(ActionType.FAN_RESET)
        enforcer.consume(ActionType.FAN_RESET)
        assert not enforcer.allows(ActionType.FAN_RESET)

    def test_consume_decrements_remaining(self):
        enforcer = AgentBudgetEnforcer()
        enforcer.update_from_lease({"FAN_RESET": 3}, stop_switch=False)
        enforcer.consume(ActionType.FAN_RESET)
        state = enforcer.get_state()
        assert state["FAN_RESET"]["remaining"] == 2
        assert state["FAN_RESET"]["used"] == 1

    def test_stop_switch_denies_all(self):
        enforcer = AgentBudgetEnforcer()
        enforcer.update_from_lease({"FAN_RESET": 10}, stop_switch=True)
        assert not enforcer.allows(ActionType.FAN_RESET)
        assert not enforcer.allows(ActionType.IDENTIFY_LED)

    def test_lease_refresh_updates_budget(self):
        enforcer = AgentBudgetEnforcer()
        enforcer.update_from_lease({"FAN_RESET": 2}, stop_switch=False)
        enforcer.consume(ActionType.FAN_RESET)
        enforcer.consume(ActionType.FAN_RESET)
        assert not enforcer.allows(ActionType.FAN_RESET)
        # New lease from SM refreshes budget
        enforcer.update_from_lease({"FAN_RESET": 5}, stop_switch=False)
        assert enforcer.allows(ActionType.FAN_RESET)

    def test_activate_deactivate_stop_switch(self):
        enforcer = AgentBudgetEnforcer()
        enforcer.activate_stop_switch()
        assert enforcer.stop_switch_active
        assert not enforcer.allows(ActionType.IDENTIFY_LED)
        enforcer.deactivate_stop_switch()
        assert not enforcer.stop_switch_active
        assert enforcer.allows(ActionType.IDENTIFY_LED)


# ===========================================================================
# P4: SM Autonomy Enforcement
# ===========================================================================


class TestSMAutonomy:
    def test_allows_when_no_policy(self):
        enforcer = SMAutonomyEnforcer()
        assert enforcer.allows_site_wide("FAN_RESET")

    def test_site_wide_budget_tracking(self):
        enforcer = SMAutonomyEnforcer()
        enforcer.update_policy([{
            "action_type": "POWER_CYCLE",
            "max_per_window": 2,
            "window_seconds": 3600,
        }])
        assert enforcer.allows_site_wide("POWER_CYCLE")
        enforcer.record_execution("POWER_CYCLE")
        enforcer.record_execution("POWER_CYCLE")
        assert not enforcer.allows_site_wide("POWER_CYCLE")

    def test_budget_for_agent_lease(self):
        enforcer = SMAutonomyEnforcer()
        enforcer.update_policy([{
            "action_type": "SEL_CLEAR",
            "max_per_window": 10,
            "window_seconds": 3600,
        }])
        budget = enforcer.get_budget_for_agent("agent-1")
        assert budget["SEL_CLEAR"] == 10

    def test_stop_switch_denies_all(self):
        enforcer = SMAutonomyEnforcer()
        enforcer.activate_stop_switch("admin")
        assert not enforcer.allows_site_wide("FAN_RESET")
        assert enforcer.stop_switch_active

    def test_stop_switch_deactivate(self):
        enforcer = SMAutonomyEnforcer()
        enforcer.activate_stop_switch("admin")
        enforcer.deactivate_stop_switch("admin")
        assert not enforcer.stop_switch_active
        assert enforcer.allows_site_wide("FAN_RESET")

    def test_state_reporting(self):
        enforcer = SMAutonomyEnforcer()
        enforcer.update_policy([{
            "action_type": "BMC_RESET",
            "max_per_window": 1,
            "window_seconds": 14400,
        }])
        enforcer.record_execution("BMC_RESET")
        state = enforcer.get_state()
        assert state["budgets"]["BMC_RESET"]["remaining"] == 0
        assert state["budgets"]["BMC_RESET"]["executions"] == 1


class TestSiteBudgetCounter:
    def test_remaining_with_window(self):
        counter = SiteBudgetCounter(
            action_type="TEST", max_per_window=3, window_seconds=60,
        )
        now = time.time()
        counter.executions = [now - 30, now - 10]
        assert counter.remaining == 1

    def test_unlimited_returns_minus_one(self):
        counter = SiteBudgetCounter(
            action_type="TEST", max_per_window=-1, window_seconds=60,
        )
        assert counter.remaining == -1


# ===========================================================================
# P5: Knowledge Base + Outcome Tracking
# ===========================================================================


class TestKnowledgeBase:
    def test_record_and_retrieve_outcome(self):
        kb = KnowledgeBase()
        outcome = StoredOutcome(
            action_id="a1", action_type="SEL_CLEAR",
            device_id="dev-1", outcome="SUCCESS",
        )
        kb.record_outcome(outcome)
        assert kb.total_outcomes == 1
        history = kb.get_device_history("dev-1")
        assert len(history) == 1
        assert history[0].action_id == "a1"

    def test_device_history_filtered_by_action_type(self):
        kb = KnowledgeBase()
        kb.record_outcome(StoredOutcome("a1", "SEL_CLEAR", "dev-1", "SUCCESS"))
        kb.record_outcome(StoredOutcome("a2", "FAN_RESET", "dev-1", "FAILURE"))
        kb.record_outcome(StoredOutcome("a3", "SEL_CLEAR", "dev-1", "SUCCESS"))
        history = kb.get_device_history("dev-1", action_type="SEL_CLEAR")
        assert len(history) == 2

    def test_unknown_device_returns_empty(self):
        kb = KnowledgeBase()
        assert kb.get_device_history("nonexistent") == []


class TestErrorBudget:
    def test_success_rate_computed(self):
        budget = ErrorBudgetState(
            action_type="TEST",
            success_count=9, failure_count=1, total_count=10,
        )
        assert budget.success_rate == 0.9

    def test_no_data_returns_1(self):
        budget = ErrorBudgetState(action_type="TEST")
        assert budget.success_rate == 1.0

    def test_drop_back_when_rate_below_threshold(self):
        budget = ErrorBudgetState(
            action_type="TEST",
            success_count=4, failure_count=2, total_count=6,
            min_success_rate=0.95,
        )
        assert budget.should_drop_back is True

    def test_no_drop_back_with_insufficient_data(self):
        budget = ErrorBudgetState(
            action_type="TEST",
            success_count=1, failure_count=1, total_count=2,
        )
        assert budget.should_drop_back is False

    def test_kb_tracks_error_budget(self):
        kb = KnowledgeBase(min_success_rate=0.8)
        # 4 successes, then 2 failures -> rate = 4/6 = 67% < 80%
        for i in range(4):
            kb.record_outcome(StoredOutcome(f"s{i}", "TEST", "dev-1", "SUCCESS"))
        assert not kb.is_action_type_dropped_back("TEST")

        kb.record_outcome(StoredOutcome("f1", "TEST", "dev-1", "FAILURE"))
        kb.record_outcome(StoredOutcome("f2", "TEST", "dev-1", "FAILURE"))
        assert kb.is_action_type_dropped_back("TEST")

    def test_manual_recovery(self):
        kb = KnowledgeBase(min_success_rate=0.8)
        for i in range(4):
            kb.record_outcome(StoredOutcome(f"s{i}", "TEST", "dev-1", "SUCCESS"))
        for i in range(3):
            kb.record_outcome(StoredOutcome(f"f{i}", "TEST", "dev-1", "FAILURE"))
        assert kb.is_action_type_dropped_back("TEST")

        kb.recover_error_budget("TEST")
        assert not kb.is_action_type_dropped_back("TEST")
        budget = kb.get_error_budget("TEST")
        assert budget.total_count == 0  # counters reset

    def test_get_all_budgets_reporting(self):
        kb = KnowledgeBase()
        kb.record_outcome(StoredOutcome("a1", "SEL_CLEAR", "dev-1", "SUCCESS"))
        budgets = kb.get_all_budgets()
        assert "SEL_CLEAR" in budgets
        assert budgets["SEL_CLEAR"]["total"] == 1


# ===========================================================================
# P6: Correlated-Conclusion Suppression
# ===========================================================================


class TestSuppression:
    def _make_event(self, device_id, domain_id="pdu-1", domain_kind="power",
                    event_family="power", severity="CRITICAL", ts=None):
        return CorrelationEvent(
            device_id=device_id,
            domain_id=domain_id,
            domain_kind=domain_kind,
            event_family=event_family,
            severity=severity,
            timestamp=ts or time.time(),
        )

    def test_no_suppression_single_event(self):
        engine = SuppressionEngine()
        result = engine.evaluate(self._make_event("dev-1"))
        assert result is None
        assert not engine.is_suppressed("pdu-1")

    def test_power_suppression_at_2_devices(self):
        """Path 1: direct dependency, power domain, threshold=2."""
        engine = SuppressionEngine()
        engine.evaluate(self._make_event("dev-1"))
        result = engine.evaluate(self._make_event("dev-2"))
        assert result is not None
        assert result.trigger_reason == "direct_dependency"
        assert result.device_count == 2
        assert engine.is_suppressed("pdu-1")

    def test_thermal_suppression_at_3_devices(self):
        """Path 1: direct dependency, cooling domain, threshold=3."""
        engine = SuppressionEngine()
        for i in range(2):
            engine.evaluate(self._make_event(
                f"dev-{i}", domain_id="cool-1", domain_kind="cooling",
                event_family="thermal",
            ))
        assert not engine.is_suppressed("cool-1")
        result = engine.evaluate(self._make_event(
            "dev-3", domain_id="cool-1", domain_kind="cooling",
            event_family="thermal",
        ))
        assert result is not None
        assert engine.is_suppressed("cool-1")

    def test_component_suppression_at_5_devices(self):
        """Path 2: statistical, component family, threshold=5."""
        engine = SuppressionEngine()
        for i in range(4):
            engine.evaluate(self._make_event(
                f"dev-{i}", domain_id="rack-1", domain_kind="rack",
                event_family="component",
            ))
        assert not engine.is_suppressed("rack-1")
        result = engine.evaluate(self._make_event(
            "dev-5", domain_id="rack-1", domain_kind="rack",
            event_family="component",
        ))
        assert result is not None
        assert result.trigger_reason == "statistical_threshold"

    def test_suppressed_domains_list(self):
        engine = SuppressionEngine()
        engine.evaluate(self._make_event("dev-1"))
        engine.evaluate(self._make_event("dev-2"))
        domains = engine.get_suppressed_domains()
        assert "pdu-1" in domains

    def test_already_suppressed_returns_existing_state(self):
        engine = SuppressionEngine()
        engine.evaluate(self._make_event("dev-1"))
        engine.evaluate(self._make_event("dev-2"))  # triggers
        result = engine.evaluate(self._make_event("dev-3"))  # already active
        assert result is not None
        assert result.device_count == 2  # original count, not 3

    def test_auto_recovery_after_stability(self):
        engine = SuppressionEngine()
        engine.evaluate(self._make_event("dev-1"))
        engine.evaluate(self._make_event("dev-2"))
        assert engine.is_suppressed("pdu-1")

        # All clear but not yet stable
        engine.check_auto_recovery("pdu-1", all_devices_healthy=True)
        assert engine.is_suppressed("pdu-1")  # stability period not elapsed

        # Simulate stability period elapsed
        state = engine._active["pdu-1"]
        state.all_clear_at = time.time() - STABILITY_PERIOD - 1
        recovered = engine.check_auto_recovery("pdu-1", all_devices_healthy=True)
        assert recovered
        assert not engine.is_suppressed("pdu-1")

    def test_auto_recovery_resets_on_unhealthy(self):
        engine = SuppressionEngine()
        engine.evaluate(self._make_event("dev-1"))
        engine.evaluate(self._make_event("dev-2"))

        engine.check_auto_recovery("pdu-1", all_devices_healthy=True)
        state = engine._active["pdu-1"]
        assert state.all_clear_at is not None

        # Devices go unhealthy again: reset timer
        engine.check_auto_recovery("pdu-1", all_devices_healthy=False)
        assert state.all_clear_at is None

    def test_human_re_enable(self):
        engine = SuppressionEngine()
        engine.evaluate(self._make_event("dev-1"))
        engine.evaluate(self._make_event("dev-2"))
        assert engine.is_suppressed("pdu-1")

        result = engine.human_re_enable("pdu-1", "admin-user")
        assert result is True
        assert not engine.is_suppressed("pdu-1")

    def test_human_re_enable_nonexistent_returns_false(self):
        engine = SuppressionEngine()
        assert not engine.human_re_enable("nonexistent", "admin")

    def test_hair_trigger_after_auto_recovery(self):
        """After auto-recovery, any re-trigger in 1h suppresses immediately."""
        engine = SuppressionEngine()
        engine.evaluate(self._make_event("dev-1"))
        engine.evaluate(self._make_event("dev-2"))

        # Force auto-recovery
        state = engine._active["pdu-1"]
        state.all_clear_at = time.time() - STABILITY_PERIOD - 1
        engine.check_auto_recovery("pdu-1", all_devices_healthy=True)
        assert not engine.is_suppressed("pdu-1")

        # Single event in same domain -> hair-trigger suppression
        result = engine.evaluate(self._make_event("dev-1"))
        assert result is not None
        assert result.trigger_reason == "hair_trigger_re-suppression"
        assert engine.is_suppressed("pdu-1")

    def test_different_domains_independent(self):
        engine = SuppressionEngine()
        engine.evaluate(self._make_event("dev-1", domain_id="pdu-1"))
        engine.evaluate(self._make_event("dev-2", domain_id="pdu-1"))
        assert engine.is_suppressed("pdu-1")
        assert not engine.is_suppressed("pdu-2")

    def test_state_reporting(self):
        engine = SuppressionEngine()
        engine.evaluate(self._make_event("dev-1"))
        engine.evaluate(self._make_event("dev-2"))
        state = engine.get_state()
        assert "pdu-1" in state["active_suppressions"]
        assert state["active_suppressions"]["pdu-1"]["event_family"] == "power"
