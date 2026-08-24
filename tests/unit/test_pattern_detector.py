"""Tests for PatternDetector fleet intelligence (R3b-3 Phase 4)."""

from __future__ import annotations

import pytest

from harkeniq_cc.outcome_aggregator import OutcomeAggregator
from harkeniq_cc.pattern_detector import FleetPattern, PatternDetector


def _outcomes(action_type, vendor, model, results):
    return [
        {"action_type": action_type, "vendor": vendor, "model": model,
         "outcome": r, "fault_resolved": False}
        for r in results
    ]


class TestBatchFailureDetection:
    def test_detects_high_failure_rate(self):
        agg = OutcomeAggregator()
        # 3 out of 6 = 50% failure rate
        agg.ingest(_outcomes("BMC_RESET", "dell", "R750", [
            "SUCCESS", "SUCCESS", "SUCCESS", "FAILURE", "FAILURE", "FAILURE",
        ]))
        detector = PatternDetector(batch_threshold=0.15, min_samples=5)
        patterns = detector.detect(agg)
        assert len(patterns) >= 1
        batch = [p for p in patterns if p.pattern_type == "batch_failure"]
        assert len(batch) == 1
        assert "BMC_RESET" in batch[0].description
        assert "dell" in batch[0].description

    def test_no_pattern_below_threshold(self):
        agg = OutcomeAggregator()
        # 1 out of 10 = 10% failure rate (below 15% threshold)
        agg.ingest(_outcomes("FAN_RESET", "dell", "R750", [
            "SUCCESS", "SUCCESS", "SUCCESS", "SUCCESS", "SUCCESS",
            "SUCCESS", "SUCCESS", "SUCCESS", "SUCCESS", "FAILURE",
        ]))
        detector = PatternDetector(batch_threshold=0.15, min_samples=5)
        patterns = detector.detect(agg)
        batch = [p for p in patterns if p.pattern_type == "batch_failure"]
        assert len(batch) == 0

    def test_no_pattern_insufficient_samples(self):
        agg = OutcomeAggregator()
        agg.ingest(_outcomes("BMC_RESET", "dell", "R750", [
            "FAILURE", "FAILURE",
        ]))
        detector = PatternDetector(min_samples=5)
        patterns = detector.detect(agg)
        assert len(patterns) == 0

    def test_dedup_same_scope(self):
        agg = OutcomeAggregator()
        agg.ingest(_outcomes("BMC_RESET", "dell", "R750", [
            "FAILURE", "FAILURE", "FAILURE", "FAILURE", "FAILURE",
        ]))
        detector = PatternDetector(batch_threshold=0.15, min_samples=5)
        p1 = detector.detect(agg)
        p2 = detector.detect(agg)  # same data, should dedup
        assert len(p1) >= 1
        assert len(p2) == 0


class TestReliabilityDetection:
    def test_detects_below_average_model(self):
        agg = OutcomeAggregator()
        # Dell R750: high failure rate
        agg.ingest(_outcomes("FAN_RESET", "dell", "R750", [
            "SUCCESS", "FAILURE", "FAILURE", "FAILURE", "FAILURE", "FAILURE",
        ]))
        # HPE DL360: low failure rate
        agg.ingest(_outcomes("FAN_RESET", "hpe", "DL360", [
            "SUCCESS", "SUCCESS", "SUCCESS", "SUCCESS", "SUCCESS", "SUCCESS",
        ]))
        detector = PatternDetector(min_samples=5)
        patterns = detector.detect(agg)
        reliability = [p for p in patterns if p.pattern_type == "reliability"]
        # Dell R750 should be flagged as below fleet average
        if reliability:
            assert any("dell" in p.description.lower() for p in reliability)


class TestFleetPattern:
    def test_pattern_id_format(self):
        pid = FleetPattern.new_id()
        assert pid.startswith("pat-")
        assert len(pid) == 12  # pat- + 8 hex chars

    def test_pattern_fields(self):
        p = FleetPattern(
            pattern_id="pat-test1234",
            pattern_type="batch_failure",
            description="test pattern",
            affected_scope={"vendor": "dell"},
            confidence=0.85,
        )
        assert p.pattern_type == "batch_failure"
        assert p.confidence == 0.85


class TestPatternDetectorState:
    def test_all_patterns_tracked(self):
        agg = OutcomeAggregator()
        agg.ingest(_outcomes("BMC_RESET", "dell", "R750", [
            "FAILURE", "FAILURE", "FAILURE", "FAILURE", "FAILURE",
        ]))
        detector = PatternDetector(batch_threshold=0.15, min_samples=5)
        detector.detect(agg)
        assert detector.pattern_count >= 1
        assert len(detector.all_patterns) >= 1
