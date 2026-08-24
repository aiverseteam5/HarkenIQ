"""Tests for OutcomeAggregator (R3b-3 Phase 4)."""

from __future__ import annotations

import pytest

from harkeniq_cc.outcome_aggregator import OutcomeAggregator


def _outcomes(action_type, vendor, model, results):
    """Helper: create outcome dicts from a list of (outcome, fault_resolved) tuples."""
    return [
        {
            "action_type": action_type,
            "vendor": vendor,
            "model": model,
            "outcome": r[0],
            "fault_resolved": r[1] if len(r) > 1 else False,
        }
        for r in results
    ]


class TestOutcomeAggregation:
    def test_ingest_counts(self):
        agg = OutcomeAggregator()
        count = agg.ingest(_outcomes("FAN_RESET", "dell", "R750", [
            ("SUCCESS",), ("SUCCESS",), ("FAILURE",),
        ]))
        assert count == 3
        metrics = agg.get_metrics()
        assert len(metrics) == 1
        assert metrics[0].total_count == 3
        assert metrics[0].success_count == 2
        assert metrics[0].failure_count == 1

    def test_success_rate(self):
        agg = OutcomeAggregator()
        agg.ingest(_outcomes("SEL_CLEAR", "dell", "R750", [
            ("SUCCESS",), ("SUCCESS",), ("SUCCESS",), ("FAILURE",),
        ]))
        m = agg.get_metrics()[0]
        assert m.success_rate == pytest.approx(0.75)
        assert m.failure_rate == pytest.approx(0.25)

    def test_empty_returns_full_rates(self):
        agg = OutcomeAggregator()
        assert agg.get_fleet_success_rate("FAN_RESET") == 1.0

    def test_partial_counted(self):
        agg = OutcomeAggregator()
        agg.ingest(_outcomes("BMC_RESET", "hpe", "DL360", [("PARTIAL",)]))
        m = agg.get_metrics()[0]
        assert m.partial_count == 1
        assert m.success_count == 0

    def test_fault_resolved_tracking(self):
        agg = OutcomeAggregator()
        agg.ingest(_outcomes("FAN_RESET", "dell", "R750", [
            ("SUCCESS", True), ("SUCCESS", False), ("SUCCESS", True),
        ]))
        m = agg.get_metrics()[0]
        assert m.fault_resolved_count == 2
        assert m.resolution_rate == pytest.approx(2 / 3)


class TestAggregatorFiltering:
    def test_filter_by_action_type(self):
        agg = OutcomeAggregator()
        agg.ingest(_outcomes("FAN_RESET", "dell", "R750", [("SUCCESS",)]))
        agg.ingest(_outcomes("SEL_CLEAR", "dell", "R750", [("SUCCESS",)]))
        result = agg.get_metrics(action_type="FAN_RESET")
        assert len(result) == 1
        assert result[0].action_type == "FAN_RESET"

    def test_filter_by_vendor(self):
        agg = OutcomeAggregator()
        agg.ingest(_outcomes("FAN_RESET", "dell", "R750", [("SUCCESS",)]))
        agg.ingest(_outcomes("FAN_RESET", "hpe", "DL360", [("SUCCESS",)]))
        result = agg.get_metrics(vendor="dell")
        assert len(result) == 1

    def test_group_count(self):
        agg = OutcomeAggregator()
        agg.ingest(_outcomes("FAN_RESET", "dell", "R750", [("SUCCESS",)]))
        agg.ingest(_outcomes("FAN_RESET", "hpe", "DL360", [("SUCCESS",)]))
        agg.ingest(_outcomes("SEL_CLEAR", "dell", "R750", [("SUCCESS",)]))
        assert agg.group_count == 3


class TestFleetSuccessRate:
    def test_fleet_wide(self):
        agg = OutcomeAggregator()
        agg.ingest(_outcomes("FAN_RESET", "dell", "R750", [
            ("SUCCESS",), ("SUCCESS",), ("FAILURE",),
        ]))
        agg.ingest(_outcomes("FAN_RESET", "hpe", "DL360", [
            ("SUCCESS",), ("SUCCESS",),
        ]))
        # Fleet: 4 success / 5 total = 0.8
        assert agg.get_fleet_success_rate("FAN_RESET") == pytest.approx(0.8)


class TestTrendDetection:
    def test_no_trend_without_history(self):
        agg = OutcomeAggregator()
        agg.ingest(_outcomes("FAN_RESET", "dell", "R750", [("FAILURE",)]))
        assert agg.get_trend(("FAN_RESET", "dell", "R750")) is None

    def test_positive_trend(self):
        agg = OutcomeAggregator()
        # Snapshot 1: low failure
        agg.ingest(_outcomes("FAN_RESET", "dell", "R750", [
            ("SUCCESS",), ("SUCCESS",), ("SUCCESS",), ("SUCCESS",), ("FAILURE",),
        ]))
        agg.snapshot()
        # Snapshot 2: higher failure
        agg.ingest(_outcomes("FAN_RESET", "dell", "R750", [
            ("FAILURE",), ("FAILURE",), ("FAILURE",),
        ]))
        agg.snapshot()
        # Snapshot 3: even higher
        agg.ingest(_outcomes("FAN_RESET", "dell", "R750", [
            ("FAILURE",), ("FAILURE",),
        ]))
        agg.snapshot()

        trend = agg.get_trend(("FAN_RESET", "dell", "R750"), window=3)
        assert trend is not None
        assert trend > 0  # increasing failure rate
