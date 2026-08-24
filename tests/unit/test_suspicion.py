"""Tests for SuspicionTracker (R3b-2 Phase 5, spec R-M20 through R-M22)."""

from __future__ import annotations

import pytest

from harkeniq.autonomy.suspicion import SuspicionTracker


class TestSuspicionAccumulation:
    def test_local_update_sets_score(self):
        tracker = SuspicionTracker(my_agent_id="me")
        tracker.update_local("fan_0", 0.5, now=100.0)
        assert tracker.get_combined_score("fan_0") == pytest.approx(0.5)

    def test_score_clamped_to_max(self):
        tracker = SuspicionTracker(my_agent_id="me")
        tracker.update_local("fan_0", 1.5, now=100.0)
        assert tracker.get_combined_score("fan_0") == pytest.approx(1.0)

    def test_threshold_triggers_claim(self):
        tracker = SuspicionTracker(
            my_agent_id="me", claim_threshold=0.8, decay_rate=0.0
        )
        tracker.update_local("fan_0", 0.9, now=100.0)
        tracker.receive_peer("fan_0", 0.9, "peer-a", now=100.0)

        triggered = tracker.tick(now=100.0)
        assert "fan_0" in triggered

    def test_single_observer_no_trigger(self):
        """R-M13: need at least 2 observers."""
        tracker = SuspicionTracker(
            my_agent_id="me", claim_threshold=0.8, decay_rate=0.0
        )
        tracker.update_local("fan_0", 0.9, now=100.0)
        # Only 1 observer
        triggered = tracker.tick(now=100.0)
        assert "fan_0" not in triggered


class TestSuspicionExchange:
    def test_peer_suspicion_merged(self):
        tracker = SuspicionTracker(my_agent_id="me", decay_rate=0.0)
        tracker.update_local("disk_1", 0.3, now=100.0)
        tracker.receive_peer("disk_1", 0.7, "peer-a", now=100.0)
        # Combined = max(0.3, 0.7) = 0.7
        assert tracker.get_combined_score("disk_1") == pytest.approx(0.7)

    def test_multiple_peers_converge(self):
        tracker = SuspicionTracker(my_agent_id="me", decay_rate=0.0)
        tracker.update_local("psu_0", 0.6, now=100.0)
        tracker.receive_peer("psu_0", 0.5, "peer-a", now=100.0)
        tracker.receive_peer("psu_0", 0.8, "peer-b", now=100.0)
        assert tracker.get_observer_count("psu_0") == 3
        assert tracker.get_combined_score("psu_0") == pytest.approx(0.8)

    def test_get_exchange_data(self):
        tracker = SuspicionTracker(my_agent_id="me", decay_rate=0.0)
        tracker.update_local("fan_0", 0.5, now=100.0)
        tracker.receive_peer("disk_1", 0.3, "peer-a", now=100.0)
        data = tracker.get_exchange_data()
        # Only local observations
        assert len(data) == 1
        assert data[0] == ("fan_0", 0.5)


class TestSuspicionDecay:
    def test_scores_decay_over_time(self):
        tracker = SuspicionTracker(my_agent_id="me", decay_rate=0.1)
        tracker.update_local("fan_0", 1.0, now=100.0)
        tracker.tick(now=110.0)  # 10 seconds, decay = 10 * 0.1 = 1.0
        assert tracker.get_combined_score("fan_0") == pytest.approx(0.0)

    def test_no_double_trigger(self):
        tracker = SuspicionTracker(
            my_agent_id="me", claim_threshold=0.8, decay_rate=0.0
        )
        tracker.update_local("fan_0", 0.9, now=100.0)
        tracker.receive_peer("fan_0", 0.9, "peer-a", now=100.0)
        triggered1 = tracker.tick(now=100.0)
        triggered2 = tracker.tick(now=101.0)
        assert "fan_0" in triggered1
        assert "fan_0" not in triggered2  # already claimed


class TestSetCover:
    def test_single_faulty_component(self):
        """R-M21: one component explains all degraded paths."""
        result = SuspicionTracker.smallest_explaining_set(
            degraded_paths=[{"A", "B"}, {"A", "C"}],
            healthy_paths=[{"B", "D"}, {"C", "D"}],
        )
        # A is on both degraded paths and NOT on any healthy path
        assert result == {"A"}

    def test_healthy_paths_exonerate(self):
        """R-M21: healthy paths carry diagnostic weight."""
        result = SuspicionTracker.smallest_explaining_set(
            degraded_paths=[{"A", "B"}, {"B", "C"}],
            healthy_paths=[{"A", "D"}],  # A is exonerated
        )
        # A is exonerated (on healthy path), B is the common factor
        assert result == {"B"}

    def test_no_consistent_explanation(self):
        result = SuspicionTracker.smallest_explaining_set(
            degraded_paths=[{"A", "B"}],
            healthy_paths=[{"A", "B"}],  # same components healthy elsewhere
        )
        assert result == set()

    def test_empty_degraded(self):
        result = SuspicionTracker.smallest_explaining_set(
            degraded_paths=[],
            healthy_paths=[{"A"}],
        )
        assert result == set()


class TestBundleCoverage:
    def test_full_coverage(self):
        """R-M22: all members measured → no gaps."""
        tracker = SuspicionTracker(my_agent_id="me")
        tracker.register_bundle("bond0", total_members=2)
        tracker.record_bundle_measurement("bond0", "member-0")
        tracker.record_bundle_measurement("bond0", "member-1")
        gaps = tracker.check_bundle_coverage("bond0")
        assert gaps is None  # fully covered

    def test_partial_coverage(self):
        """R-M22: missing measurement on one member."""
        tracker = SuspicionTracker(my_agent_id="me")
        tracker.register_bundle("bond0", total_members=2)
        tracker.record_bundle_measurement("bond0", "member-0")
        gaps = tracker.check_bundle_coverage("bond0")
        assert gaps is not None
        assert len(gaps) == 1
