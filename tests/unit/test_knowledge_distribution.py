"""Tests for KnowledgeDistributor (R3b-3 Phase 6, R-C1)."""

from __future__ import annotations

import json

import pytest

from harkeniq_cc.knowledge_distributor import KnowledgeDistributor
from harkeniq_cc.pattern_detector import FleetPattern


def _pattern(pattern_id="pat-1", vendor="dell", model="R750"):
    return FleetPattern(
        pattern_id=pattern_id,
        pattern_type="batch_failure",
        description=f"BMC_RESET fails on {vendor} {model}",
        affected_scope={"vendor": vendor, "model": model, "action_type": "BMC_RESET"},
        confidence=0.9,
        evidence={"failure_rate": 0.3},
    )


def _site(site_id, devices):
    return {"site_id": site_id, "devices": devices}


class TestTargetSelection:
    def test_matches_vendor_model(self):
        dist = KnowledgeDistributor()
        pattern = _pattern(vendor="dell", model="R750")
        sites = [
            _site("s1", [{"vendor": "dell", "model": "R750"}]),
            _site("s2", [{"vendor": "hpe", "model": "DL360"}]),
        ]
        targets = dist.select_targets(pattern, sites)
        assert len(targets) == 1
        assert targets[0]["site_id"] == "s1"

    def test_no_match(self):
        dist = KnowledgeDistributor()
        pattern = _pattern(vendor="dell", model="R750")
        sites = [
            _site("s1", [{"vendor": "hpe", "model": "DL360"}]),
        ]
        targets = dist.select_targets(pattern, sites)
        assert len(targets) == 0

    def test_multiple_matches(self):
        dist = KnowledgeDistributor()
        pattern = _pattern(vendor="dell", model="R750")
        sites = [
            _site("s1", [{"vendor": "dell", "model": "R750"}]),
            _site("s2", [{"vendor": "dell", "model": "R750"}, {"vendor": "hpe", "model": "DL360"}]),
        ]
        targets = dist.select_targets(pattern, sites)
        assert len(targets) == 2


class TestPayloadPreparation:
    def test_serializes_patterns(self):
        dist = KnowledgeDistributor()
        patterns = [_pattern("pat-1"), _pattern("pat-2")]
        payload = dist.prepare_payload(patterns)
        data = json.loads(payload)
        assert len(data) == 2
        assert data[0]["pattern_id"] == "pat-1"

    def test_excludes_already_distributed(self):
        dist = KnowledgeDistributor()
        p1 = _pattern("pat-1")
        dist.record_distribution(p1, "s1")
        patterns = [p1, _pattern("pat-2")]
        payload = dist.prepare_payload(patterns)
        data = json.loads(payload)
        assert len(data) == 1
        assert data[0]["pattern_id"] == "pat-2"


class TestDistributionTracking:
    def test_record_distribution(self):
        dist = KnowledgeDistributor()
        p = _pattern()
        dist.record_distribution(p, "s1")
        assert dist.distribution_count == 1

    def test_undistributed_patterns(self):
        dist = KnowledgeDistributor()
        p1 = _pattern("pat-1")
        p2 = _pattern("pat-2")
        dist.record_distribution(p1, "s1")
        undist = dist.undistributed_patterns([p1, p2])
        assert len(undist) == 1
        assert undist[0].pattern_id == "pat-2"

    def test_distribution_history(self):
        dist = KnowledgeDistributor()
        dist.record_distribution(_pattern("pat-1"), "s1")
        dist.record_distribution(_pattern("pat-2"), "s2")
        history = dist.distribution_history
        assert len(history) == 2
        assert history[0].site_id == "s1"
