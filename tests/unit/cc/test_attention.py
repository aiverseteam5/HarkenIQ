"""S2: the attention capability — ranking, explanation, recommendation.

These pin the CONTRACT an agent will consume, not just the page that
renders it. The composer is pure, so most of this needs no database.

What matters here:
  * ranking is deterministic and stable (two polls agree);
  * every item carries site attribution, or a site-scoped agent cannot
    use the capability at all;
  * confidence is honest — insufficient_data never becomes a number;
  * recommendations are deterministic, name real capabilities, and never
    confer authority;
  * evidence is traceable to inputs, never invented.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from harkeniq_cc.attention import build_attention
from harkeniq_cc.predictive import DeviceRisk

TENANT = "t1"


def _dev(agent_id="a1", site_id="s1", vendor="Dell", model="R750",
         health="ok", tag="TAG1"):
    return SimpleNamespace(
        id=f"id-{agent_id}", agent_id=agent_id, agent_name=f"srv-{agent_id}",
        site_id=site_id, vendor=vendor, model=model, health=health,
        observation="observed", device_class="server", service_tag=tag,
        firmware=[], subsystems={},
    )


def _risk(agent_id="a1", score=0.7, band="high", basis="device_history",
          samples=10, site_id="s1", **factors):
    f = {"basis": basis, **factors}
    if basis == "device_history":
        f.setdefault("weighted_failure_rate", score)
    elif basis == "cohort_prior":
        f.setdefault("cohort_failure_rate", score)
    return DeviceRisk(
        agent_id=agent_id, vendor="Dell", model="R750", risk_score=score,
        band=band, sample_count=samples, factors=f, site_id=site_id,
        agent_name=f"srv-{agent_id}",
    )


def _site(sid="s1", name="BLR-1"):
    return SimpleNamespace(id=sid, site_name=name)


def _route(action_id="act-1", agent_id="a1", action_type="SEL_CLEAR"):
    return SimpleNamespace(
        action_id=action_id, device_agent_id=agent_id,
        action_type=action_type, routed_at=None,
    )


def _pattern(ptype="anomaly", vendor="Dell", model="R750"):
    return SimpleNamespace(
        pattern_id="p1", pattern_type=ptype, description="rate rising",
        confidence=0.7, affected_scope={"vendor": vendor, "model": model},
    )


def _build(**over):
    args = dict(
        devices=[_dev()], risks=[_risk()], exposures=[], warranty_map={},
        pending_routes=[], patterns=[], sites=[_site()], tenant_id=TENANT,
    )
    args.update(over)
    return build_attention(**args)


class TestRankingIsDeterministic:
    def test_high_outranks_medium_outranks_insufficient(self):
        res = _build(
            devices=[_dev("a1"), _dev("a2"), _dev("a3")],
            risks=[
                _risk("a3", 0.0, "insufficient_data", basis="insufficient_data",
                      samples=0),
                _risk("a2", 0.4, "medium"),
                _risk("a1", 0.9, "high"),
            ],
        )
        assert [i["agent_id"] for i in res["items"]] == ["a1", "a2", "a3"]
        assert [i["rank"] for i in res["items"]] == [1, 2, 3]

    def test_equal_scores_break_ties_stably(self):
        """Two polls must not swap equal devices — an operator watching a
        list that reshuffles itself stops trusting it."""
        risks = [_risk("b2", 0.5, "medium"), _risk("b1", 0.5, "medium")]
        first = _build(devices=[_dev("b1"), _dev("b2")], risks=risks)
        second = _build(devices=[_dev("b2"), _dev("b1")], risks=list(reversed(risks)))
        assert [i["agent_id"] for i in first["items"]] == \
               [i["agent_id"] for i in second["items"]] == ["b1", "b2"]


class TestAttentionOrderIsNotJustRiskBand:
    """Found on the live stack during S2: a device reporting CRITICAL health
    scored band "low", because the predictive model scores FUTURE failure
    and this device had no failure history. Ranking on band alone buried a
    currently-broken machine below a healthy one with a flaky past — which
    fails the question the surface exists to answer."""

    def test_currently_failing_outranks_higher_predicted_risk(self):
        res = _build(
            devices=[_dev("broken", health="critical"), _dev("flaky", health="ok")],
            risks=[
                _risk("broken", 0.2, "low"),     # low PREDICTED risk
                _risk("flaky", 0.9, "high"),     # high predicted risk, healthy now
            ],
        )
        assert [i["agent_id"] for i in res["items"]] == ["broken", "flaky"]
        assert res["items"][0]["attention_driver"] == "current_failure"
        assert res["items"][1]["attention_driver"] == "predicted_risk"

    def test_the_failing_device_leads_with_its_present_tense_trouble(self):
        res = _build(devices=[_dev("broken", health="critical")],
                     risks=[_risk("broken", 0.2, "low")])
        assert "CRITICAL health right now" in res["items"][0]["reasons"][0]

    def test_awaiting_approval_outranks_plain_predicted_risk(self):
        res = _build(
            devices=[_dev("waiting"), _dev("risky")],
            risks=[_risk("waiting", 0.1, "low"), _risk("risky", 0.8, "high")],
            pending_routes=[_route("act-1", "waiting")],
        )
        assert [i["agent_id"] for i in res["items"]] == ["waiting", "risky"]
        assert res["items"][0]["attention_driver"] == "awaiting_approval"

    def test_failing_outranks_awaiting_approval(self):
        res = _build(
            devices=[_dev("broken", health="critical"), _dev("waiting")],
            risks=[_risk("broken", 0.2, "low"), _risk("waiting", 0.2, "low")],
            pending_routes=[_route("act-1", "waiting")],
        )
        assert [i["agent_id"] for i in res["items"]] == ["broken", "waiting"]

    def test_needs_attention_counts_broken_devices_whatever_the_band(self):
        res = _build(
            devices=[_dev("broken", health="critical"), _dev("fine")],
            risks=[_risk("broken", 0.2, "low"), _risk("fine", 0.05, "low")],
        )
        assert res["summary"]["attention_required"] == 1
        assert res["sites"][0]["needs_attention"] == 1

    def test_band_is_untouched_by_the_ordering_change(self):
        """The predictive model is not re-interpreted: the band a device
        gets is still exactly what score_device produced."""
        res = _build(devices=[_dev("broken", health="critical")],
                     risks=[_risk("broken", 0.2, "low")])
        assert res["items"][0]["band"] == "low"
        assert res["items"][0]["risk_score"] == 0.2

    def test_driver_is_explained_in_words(self):
        res = _build(devices=[_dev("broken", health="critical")],
                     risks=[_risk("broken", 0.2, "low")])
        assert res["items"][0]["attention_driver_label"] == "Failing now"


class TestSiteAttribution:
    """Without this a site-scoped agent cannot use the capability."""

    def test_every_item_carries_site_id_and_name(self):
        res = _build(sites=[_site("s1", "BLR-1")])
        assert res["items"][0]["site_id"] == "s1"
        assert res["items"][0]["site_name"] == "BLR-1"

    def test_site_rollup_counts_bands_and_evidence(self):
        res = _build(
            devices=[_dev("a1", "s1"), _dev("a2", "s1"), _dev("a3", "s2")],
            risks=[
                _risk("a1", 0.9, "high", site_id="s1"),
                _risk("a2", 0.4, "medium", site_id="s1"),
                _risk("a3", 0.1, "low", site_id="s2"),
            ],
            sites=[_site("s1", "BLR-1"), _site("s2", "PUN-1")],
            pending_routes=[_route("act-1", "a1")],
        )
        by_id = {s["site_id"]: s for s in res["sites"]}
        assert by_id["s1"]["device_count"] == 2
        assert by_id["s1"]["by_band"]["high"] == 1
        assert by_id["s1"]["by_band"]["medium"] == 1
        assert by_id["s1"]["pending_approvals"] == 1
        assert by_id["s2"]["by_band"]["low"] == 1
        # Worst site first, so "where do I look" is the top of the list.
        assert res["sites"][0]["site_id"] == "s1"

    def test_rollup_and_items_cannot_disagree(self):
        res = _build(
            devices=[_dev("a1"), _dev("a2")],
            risks=[_risk("a1", 0.9, "high"), _risk("a2", 0.2, "low")],
        )
        rollup_total = sum(s["device_count"] for s in res["sites"])
        assert rollup_total == len(res["items"]) == res["summary"]["devices_scored"]


class TestConfidenceIsHonest:
    def test_insufficient_data_is_named_not_scored_away(self):
        res = _build(risks=[_risk(score=0.0, band="insufficient_data",
                                  basis="insufficient_data", samples=0)])
        item = res["items"][0]
        assert item["band"] == "insufficient_data"
        assert item["confidence"]["sufficient"] is False
        assert item["confidence"]["sample_count"] == 0
        assert "not" in item["confidence"]["explanation"].lower()
        # It must not read as a clean bill of health.
        assert any("not proven healthy" in r for r in item["reasons"])

    def test_cohort_basis_is_disclosed_as_borrowed_evidence(self):
        res = _build(risks=[_risk(basis="cohort_prior", samples=2, score=0.3)])
        item = res["items"][0]
        assert item["confidence"]["basis"] == "cohort_prior"
        assert item["confidence"]["sufficient"] is False
        assert any("peers" in r for r in item["reasons"])

    def test_device_history_is_the_only_sufficient_basis(self):
        res = _build(risks=[_risk(basis="device_history", samples=12)])
        assert res["items"][0]["confidence"]["sufficient"] is True


class TestEvidenceIsTraceable:
    def test_cves_are_attached_to_their_device_only(self):
        res = _build(
            devices=[_dev("a1"), _dev("a2")],
            risks=[_risk("a1"), _risk("a2", 0.2, "low")],
            exposures=[{
                "agent_id": "a1", "cve_id": "CVE-2026-1", "severity": "critical",
                "component": "bios", "component_name": "BIOS", "version": "1.0",
                "fixed_version": "2.0",
            }],
        )
        by_agent = {i["agent_id"]: i for i in res["items"]}
        assert len(by_agent["a1"]["evidence"]["cves"]) == 1
        assert by_agent["a2"]["evidence"]["cves"] == []
        assert any("CVE-2026-1" in r for r in by_agent["a1"]["reasons"])

    def test_cohort_pattern_is_reported_as_a_fleet_signal(self):
        res = _build(patterns=[_pattern("anomaly")])
        item = res["items"][0]
        assert item["evidence"]["fleet_patterns"][0]["pattern_type"] == "anomaly"
        assert any("Fleet-wide" in r for r in item["reasons"])

    def test_patterns_for_other_models_are_not_attached(self):
        res = _build(patterns=[_pattern("anomaly", vendor="HPE", model="DL380")])
        assert res["items"][0]["evidence"]["fleet_patterns"] == []

    def test_no_risk_trend_is_invented(self):
        """Risk history is not persisted, so no item may claim a device-level
        trend. Cohort anomaly evidence is allowed and is labelled as such."""
        item = _build()["items"][0]
        assert "trend" not in item
        assert "risk_trend" not in item


class TestRecommendationIsGovernedAndDeterministic:
    def test_pending_approval_outranks_everything(self):
        res = _build(
            exposures=[{"agent_id": "a1", "cve_id": "CVE-1", "severity": "high",
                        "component": "bios", "component_name": "BIOS",
                        "version": "1.0", "fixed_version": "2.0"}],
            pending_routes=[_route("act-9", "a1")],
        )
        rec = res["items"][0]["recommended_next"]
        assert rec["capability"] == "review_pending_approval"
        assert rec["requires_approval"] is True
        assert rec["refs"] == ["act-9"]

    def test_fixable_cve_recommends_firmware_but_admits_it_is_not_invocable(self):
        res = _build(exposures=[{
            "agent_id": "a1", "cve_id": "CVE-2", "severity": "high",
            "component": "bios", "component_name": "BIOS", "version": "1.0",
            "fixed_version": "2.0",
        }])
        rec = res["items"][0]["recommended_next"]
        assert rec["capability"] == "plan_firmware_remediation"
        assert rec["requires_approval"] is True
        # Firmware is never budget-granted and its tenant path is a later
        # slice — the contract must not promise a door that is not cut.
        assert rec["available"] is False
        assert rec["unavailable_reason"]

    def test_insufficient_data_recommends_evidence_not_action(self):
        res = _build(risks=[_risk(band="insufficient_data",
                                  basis="insufficient_data", samples=0)])
        rec = res["items"][0]["recommended_next"]
        assert rec["capability"] == "collect_evidence"
        assert rec["requires_approval"] is False

    def test_low_risk_recommends_monitoring(self):
        rec = _build(risks=[_risk(score=0.05, band="low")])["items"][0]["recommended_next"]
        assert rec["capability"] == "monitor"

    def test_recommendation_never_carries_mutation_authority(self):
        """Read-only intelligence must not silently become an action."""
        for res in (
            _build(pending_routes=[_route()]),
            _build(risks=[_risk(band="insufficient_data",
                                basis="insufficient_data", samples=0)]),
            _build(),
        ):
            rec = res["items"][0]["recommended_next"]
            assert set(rec) <= {
                "capability", "summary", "requires_approval", "available",
                "unavailable_reason", "refs",
            }
            # No token, no url-to-POST, no pre-authorized handle.
            assert "token" not in rec and "authorization" not in rec


class TestSummary:
    def test_summary_counts_what_a_human_asks_first(self):
        res = _build(
            devices=[_dev("a1"), _dev("a2"), _dev("a3")],
            risks=[
                _risk("a1", 0.9, "high"),
                _risk("a2", 0.4, "medium"),
                _risk("a3", 0.0, "insufficient_data",
                      basis="insufficient_data", samples=0),
            ],
            pending_routes=[_route("act-1", "a1")],
        )
        s = res["summary"]
        assert s["devices_scored"] == 3
        assert s["attention_required"] == 2
        assert s["insufficient_data_count"] == 1
        assert s["actions_awaiting_approval"] == 1

    def test_tenant_id_is_echoed_for_the_consumer(self):
        assert _build()["tenant_id"] == TENANT
