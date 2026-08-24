"""Tests for LearningFeedbackTracker (R3b-3 Phase 7, R-C1)."""

from __future__ import annotations

import pytest

from harkeniq_cc.learning_feedback import LearningFeedbackTracker


class TestLearningCycle:
    def test_start_cycle(self):
        tracker = LearningFeedbackTracker()
        entry = tracker.start_cycle(
            "cycle-1", "pat-1", "batch_failure",
            baseline_metrics={"failure_rate": 0.3, "success_rate": 0.7},
        )
        assert entry.cycle_id == "cycle-1"
        assert entry.pattern_id == "pat-1"
        assert tracker.total_cycles == 1

    def test_record_skill_generated(self):
        tracker = LearningFeedbackTracker()
        tracker.start_cycle("c1", "p1", "batch_failure", {})
        tracker.record_skill_generated("c1", "skill-abc")
        entry = tracker.get_cycle("c1")
        assert entry.skill_id == "skill-abc"

    def test_record_distribution(self):
        tracker = LearningFeedbackTracker()
        tracker.start_cycle("c1", "p1", "batch_failure", {})
        tracker.record_distribution("c1", sites=3, devices=45)
        entry = tracker.get_cycle("c1")
        assert entry.sites_distributed == 3
        assert entry.devices_applied == 45

    def test_record_outcomes_improvement(self):
        tracker = LearningFeedbackTracker()
        tracker.start_cycle(
            "c1", "p1", "batch_failure",
            baseline_metrics={"failure_rate": 0.3},
        )
        improvement = tracker.record_outcomes(
            "c1", {"failure_rate": 0.1, "success_rate": 0.9},
        )
        # 0.3 → 0.1 = (0.3-0.1)/0.3 * 100 = 66.7%
        assert improvement == pytest.approx(66.7, abs=0.1)

    def test_record_outcomes_no_improvement(self):
        tracker = LearningFeedbackTracker()
        tracker.start_cycle(
            "c1", "p1", "batch_failure",
            baseline_metrics={"failure_rate": 0.1},
        )
        improvement = tracker.record_outcomes(
            "c1", {"failure_rate": 0.1},
        )
        assert improvement == pytest.approx(0.0)


class TestPromotion:
    def test_auto_promote_above_threshold(self):
        tracker = LearningFeedbackTracker(
            promotion_success_rate=0.95,
            promotion_min_devices=50,
        )
        tracker.start_cycle("c1", "p1", "batch_failure", {})
        tracker.record_skill_generated("c1", "skill-abc")
        tracker.record_distribution("c1", sites=5, devices=60)
        tracker.record_outcomes("c1", {"success_rate": 0.97, "failure_rate": 0.03})
        promoted = tracker.check_promotion("c1")
        assert promoted is True
        assert "skill-abc" in tracker.promotions

    def test_no_promote_below_threshold(self):
        tracker = LearningFeedbackTracker(
            promotion_success_rate=0.95,
            promotion_min_devices=50,
        )
        tracker.start_cycle("c1", "p1", "batch_failure", {})
        tracker.record_skill_generated("c1", "skill-abc")
        tracker.record_distribution("c1", sites=5, devices=60)
        tracker.record_outcomes("c1", {"success_rate": 0.80, "failure_rate": 0.20})
        promoted = tracker.check_promotion("c1")
        assert promoted is False

    def test_no_promote_insufficient_devices(self):
        tracker = LearningFeedbackTracker(
            promotion_success_rate=0.95,
            promotion_min_devices=50,
        )
        tracker.start_cycle("c1", "p1", "batch_failure", {})
        tracker.record_skill_generated("c1", "skill-abc")
        tracker.record_distribution("c1", sites=1, devices=10)
        tracker.record_outcomes("c1", {"success_rate": 0.99})
        promoted = tracker.check_promotion("c1")
        assert promoted is False

    def test_no_promote_without_skill(self):
        tracker = LearningFeedbackTracker()
        tracker.start_cycle("c1", "p1", "batch_failure", {})
        # No skill_id recorded
        promoted = tracker.check_promotion("c1")
        assert promoted is False


class TestCycleQueries:
    def test_active_cycles(self):
        tracker = LearningFeedbackTracker()
        tracker.start_cycle("c1", "p1", "batch_failure", {})
        tracker.start_cycle("c2", "p2", "anomaly", {})
        tracker.record_outcomes("c2", {"failure_rate": 0.05})
        active = tracker.get_active_cycles()
        assert len(active) == 1
        assert active[0].cycle_id == "c1"

    def test_completed_cycles(self):
        tracker = LearningFeedbackTracker()
        tracker.start_cycle("c1", "p1", "batch_failure", {})
        tracker.record_outcomes("c1", {"failure_rate": 0.05})
        completed = tracker.get_completed_cycles()
        assert len(completed) == 1

    def test_nonexistent_cycle(self):
        tracker = LearningFeedbackTracker()
        assert tracker.get_cycle("nope") is None
        assert tracker.record_outcomes("nope", {}) is None
