"""S3: learned signals — durable knowledge, evidence-bound scope.

The product test for this slice is one sentence:

    "Can something learned from yesterday materially improve what HarkenIQ
     tells a human or agent to pay attention to tomorrow?"

These tests answer it in three parts: the knowledge is derived only where
evidence supports it, it SURVIVES A RESTART, and the next attention
evaluation consumes it.

Vocabulary is kept distinct on purpose (pattern != learned signal !=
candidate != promoted capability); collapsing them is how a governed
learning substrate turns into "the AI decided".
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from harkeniq_cc.learned_signals import (
    MIN_SIGNAL_CONFIDENCE,
    cohort_ref,
    derive_signals,
    signal_key,
    signals_for_device,
)


def _pattern(ptype="batch_failure", confidence=0.6, scope=None, evidence=None):
    return SimpleNamespace(
        pattern_id=f"pat-{ptype}",
        pattern_type=ptype,
        description="d",
        confidence=confidence,
        affected_scope=scope if scope is not None else {
            "action_type": "SEL_CLEAR", "vendor": "Dell", "model": "R750",
        },
        evidence=evidence if evidence is not None else {
            "total": 20, "failures": 8, "failure_rate": 0.4,
        },
    )


class TestScopeIsEvidenceBound:
    """Rule: only where evidence supports the scope. Patterns aggregate by
    cohort, so cohort scope is always justified; site scope only when the
    pattern names the failing sites; device and tenant scope never."""

    def test_cohort_signal_is_always_derived(self):
        signals = derive_signals(_pattern())
        assert len(signals) == 1
        assert signals[0]["scope_type"] == "cohort"
        assert signals[0]["scope_ref"] == "dell/r750"

    def test_site_scope_only_from_cross_site_evidence(self):
        cross = _pattern(
            "cross_site_batch", 0.8,
            scope={"action_type": "SEL_CLEAR", "vendor": "Dell",
                   "model": "R750", "sites": "site-a,site-b"},
            evidence={"total": 30, "failures": 15, "failure_rate": 0.5,
                      "sites_affected": 2,
                      "site_failure_counts": {"site-a": 9, "site-b": 6}},
        )
        signals = derive_signals(cross)
        by_scope = {(s["scope_type"], s["scope_ref"]) for s in signals}
        assert ("cohort", "dell/r750") in by_scope
        assert ("site", "site-a") in by_scope
        assert ("site", "site-b") in by_scope

    def test_site_signal_carries_that_sites_own_evidence(self):
        cross = _pattern(
            "cross_site_batch", 0.8,
            scope={"action_type": "SEL_CLEAR", "vendor": "Dell",
                   "model": "R750", "sites": "site-a"},
            evidence={"total": 30, "failures": 15, "failure_rate": 0.5,
                      "site_failure_counts": {"site-a": 9}},
        )
        site_sig = [s for s in derive_signals(cross) if s["scope_type"] == "site"][0]
        assert site_sig["evidence"]["failures_at_site"] == 9

    def test_non_cross_site_patterns_never_produce_site_scope(self):
        for ptype in ("batch_failure", "anomaly", "reliability"):
            signals = derive_signals(_pattern(ptype))
            assert all(s["scope_type"] == "cohort" for s in signals), ptype

    def test_device_and_tenant_scope_are_never_invented(self):
        """Pattern evidence says nothing about an individual device, and a
        global signal would be exactly the over-reach the rule forbids."""
        for ptype in ("batch_failure", "anomaly", "reliability",
                      "cross_site_batch"):
            for s in derive_signals(_pattern(ptype)):
                assert s["scope_type"] in ("cohort", "site")

    def test_weak_patterns_produce_no_knowledge(self):
        assert derive_signals(_pattern(confidence=MIN_SIGNAL_CONFIDENCE - 0.01)) == []

    def test_pattern_without_a_cohort_produces_no_knowledge(self):
        """Nothing to attach the knowledge to."""
        assert derive_signals(_pattern(scope={"action_type": "SEL_CLEAR"})) == []


class TestStatementIsTraceable:
    def test_statement_quotes_the_patterns_own_numbers(self):
        s = derive_signals(_pattern())[0]
        assert "40%" in s["statement"]
        assert "8 of 20" in s["statement"]
        assert "SEL_CLEAR" in s["statement"]

    def test_anomaly_statement_speaks_of_change_not_absolute_rate(self):
        s = derive_signals(_pattern(
            "anomaly", 0.7,
            evidence={"current_failure_rate": 0.3, "trend": 0.12},
        ))[0]
        assert "more often" in s["statement"]
        assert "+12%" in s["statement"]

    def test_signal_key_is_stable_for_upsert(self):
        a = derive_signals(_pattern())[0]["signal_key"]
        b = derive_signals(_pattern())[0]["signal_key"]
        assert a == b == signal_key("cohort", cohort_ref("Dell", "R750"), "SEL_CLEAR")

    def test_cohort_ref_is_case_insensitive(self):
        assert cohort_ref("DELL", "R750") == cohort_ref("dell", "r750")


def _row(scope_type="cohort", scope_ref="dell/r750", key="k1", conf=0.6):
    return SimpleNamespace(
        signal_key=key, scope_type=scope_type, scope_ref=scope_ref,
        action_type="SEL_CLEAR", statement="stmt", confidence=conf,
        evidence={}, observation_count=3, source_pattern_id="pat-1",
        source_cycle_id="cyc-1", last_confirmed_at=None,
    )


class TestSelectionForADevice:
    def test_site_knowledge_outranks_cohort_knowledge(self):
        rows = [
            _row("cohort", "dell/r750", "cohort-key"),
            _row("site", "site-a", "site-key"),
        ]
        picked = signals_for_device(rows, "Dell", "R750", "site-a")
        assert [p["signal_key"] for p in picked] == ["site-key", "cohort-key"]

    def test_a_device_never_inherits_another_cohorts_knowledge(self):
        rows = [_row("cohort", "hpe/dl380", "other")]
        assert signals_for_device(rows, "Dell", "R750", "site-a") == []

    def test_a_device_never_inherits_another_sites_knowledge(self):
        rows = [_row("site", "site-b", "elsewhere")]
        assert signals_for_device(rows, "Dell", "R750", "site-a") == []

    def test_selection_is_case_insensitive_on_cohort(self):
        rows = [_row("cohort", "dell/r750", "k")]
        assert len(signals_for_device(rows, "DELL", "R750", "s")) == 1
