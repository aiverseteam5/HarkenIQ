"""A6-4B0b-S4 (spec A30.28): learned-signal and fleet-pattern PAYLOAD isolation.

A23 is retained: a vendor/model cohort conclusion is tenant knowledge.
E3-F1 is what lay under it -- a reader entitled to the ROW was handed its
CONTENT as stored: a hidden site's id and failure count, the number of
sites, the tenant totals, and a sentence restating all of it.

The master oracle is the TWIN ESTATE (`s4_estate`): two tenants that
differ only at a site the reader does not hold, chosen so every number
lands in one band. A scoped reader must be returned the same bytes by
both; a tenant-wide reader must NOT, or the comparison proves nothing.
Everything the payloads contain was written by the production
`IntelligenceEngine`, not by hand.
"""

from __future__ import annotations

import ast
import json
import pathlib
import re

import pytest

import harkeniq_cc
from harkeniq_cc import agent_runtime, learning_projection as LP
from harkeniq_cc.api import approvals as approvals_api
from harkeniq_cc.api import incidents as incidents_api
from harkeniq_cc.api import outcomes as outcomes_api
from harkeniq_cc.governance import (
    LearningView,
    learning_view,
    require_learning_view,
)
from harkeniq_cc.knowledge_distributor import site_payload
from harkeniq_cc.learned_signals import render_statement
from harkeniq_cc.pattern_detector import FleetPattern
from harkeniq_cc.route_contract import READ_SCOPED, ROUTE_CONTRACT
from harkeniq_cc.scope import empty_scope

from tests.unit.cc import s3_estate as E
from tests.unit.cc import s4_estate as S
from tests.unit.cc.s3_estate import SITES

#: Every scoped persona of S3's matrix that can call a `fleet.view` route.
SCOPED = [name for name, (_, holds) in E.PERSONAS.items() if holds is not None]

#: Text that states a count. None of it may reach a scoped reader from a
#: learning payload. (`INCIDENT_TELEMETRY` says "3 of 5 fans" and "95%" on
#: purpose: it is the device's own and must survive.)
COUNT_SHAPES = [
    re.compile(r"\d+ of \d+ attempts"),
    re.compile(r"across \d+ sites"),
    re.compile(r"\(\d+\s*/\s*\d+\)"),
]


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


async def _get(stack, subject, path, role="site_admin", **params) -> dict:
    res = await stack.as_person(subject, role).get(path, **params)
    assert res.status_code == 200, (path, res.status_code, res.text)
    return res.json()


async def _surfaces(stack, subject, role="site_admin", incident_id=None) -> dict:
    """Every principal-facing carrier of learned knowledge, in one dict."""
    out = {
        "signals": await _get(stack, subject, "/api/learning/signals", role),
        "cycles": await _get(stack, subject, "/api/learning/cycles", role),
        "patterns": await _get(stack, subject, "/api/outcomes/patterns", role),
        "patterns?limit=1": await _get(
            stack, subject, "/api/outcomes/patterns", role, limit=1),
        "patterns?limit=2": await _get(
            stack, subject, "/api/outcomes/patterns", role, limit=2),
        "attention": _learning_of_attention(
            await _get(stack, subject, "/api/attention/", role)),
        "autonomy.learning": _learning_of_autonomy(
            await _get(stack, subject, "/api/autonomy/", role)),
    }
    if incident_id:
        listed = await stack.as_person(subject, role).get("/api/incidents/")
        if listed.status_code == 200:
            out["incidents"] = [
                i.get("diagnosis") for i in listed.json().get("incidents", [])
            ]
        detail = await stack.as_person(subject, role).get(f"/api/incidents/{incident_id}")
        if detail.status_code == 200:
            body = detail.json()
            out["incident"] = {
                "diagnosis": body.get("diagnosis"),
                "prior_learning": body.get("prior_learning"),
            }
    return out


def _learning_of_attention(payload: dict) -> list:
    """The LEARNING carried by attention, per device, in rank order.

    A30.28 names the predictive cohort prior (`risk_score`,
    `cohort_failure_rate`, `outcomes_considered`) as S3-E2's family and a
    follow-up, so the twin comparison is over what S4 owns: the learned
    signals, the fleet patterns, and the sentences quoting them.
    """
    return [
        {
            "agent_id": item["agent_id"],
            "learned_signals": item["evidence"].get("learned_signals"),
            "fleet_patterns": item["evidence"].get("fleet_patterns"),
            "learned_reasons": [
                r for r in item.get("reasons", [])
                if "learned" in r.lower() or "fleet" in r.lower()
                or "fails" in r.lower() or "pattern" in r.lower()
            ],
        }
        for item in payload["items"]
    ]


def _learning_of_autonomy(payload: dict) -> dict:
    return {
        row["action_type"]: row.get("learning")
        for row in payload["action_classes"] if row.get("learning")
    }


async def _twins(incident: bool = True):
    x, y = await S.build("X"), await S.build("Y")
    ids = {}
    if incident:
        ids = {"X": await S.seed_cited_incident(x), "Y": await S.seed_cited_incident(y)}
    return x, y, ids


async def _compare(x, y, subject_x, subject_y, role, ids) -> tuple[dict, dict]:
    """(canonical bytes per surface for X, for Y)."""
    names_x, names_y = await S.aliases(x), await S.aliases(y)
    got_x = await _surfaces(x, subject_x, role, ids.get("X"))
    got_y = await _surfaces(y, subject_y, role, ids.get("Y"))
    return (
        {k: S.canonical(v, names_x) for k, v in got_x.items()},
        {k: S.canonical(v, names_y) for k, v in got_y.items()},
    )


def _strings(payload):
    return [leaf for _, leaf in S.walk(payload) if isinstance(leaf, str)]


# ---------------------------------------------------------------------------
# The fixture is honest
# ---------------------------------------------------------------------------


def _independent_band(value: float, step: float) -> float:
    """Round half up to a grid. Restated here, NOT imported."""
    return int(value / step + 0.5) * step


class TestTheFixtureIsHonest:
    async def test_the_twins_really_share_every_band(self):
        """Exact values DIFFER between the estates and BAND equal -- by
        arithmetic restated in this file, over rows the real engine wrote."""
        exact, banded = {}, {}
        for variant in ("X", "Y"):
            stack = await S.build(variant)
            for p in await S.patterns(stack):
                if p.pattern_type != "batch_failure":
                    continue
                action = p.affected_scope["action_type"]
                failures, total = S.EXPECTED[variant][action]
                assert (p.evidence["failures"], p.evidence["total"]) == (failures, total)
                exact.setdefault(action, []).append(
                    (p.evidence["failure_rate"], p.confidence))
                banded.setdefault(action, []).append((
                    round(_independent_band(p.evidence["failure_rate"], 0.05), 2),
                    round(_independent_band(p.confidence, 0.25), 2),
                ))
        for action in ("SEL_CLEAR", "BMC_RESET"):
            assert exact[action][0] != exact[action][1], action
            assert banded[action][0] == banded[action][1], action
        assert exact["POWER_CYCLE"][0] == exact["POWER_CYCLE"][1]
        # The order twin: 0.70 < 0.75 < 0.80, one band.
        assert exact["BMC_RESET"][0][1] < exact["POWER_CYCLE"][0][1] < exact["BMC_RESET"][1][1]
        assert {b[1] for b in banded["BMC_RESET"] + banded["POWER_CYCLE"]} == {0.75}

    async def test_what_is_stored_is_E3_F1(self):
        """CONTROL, and the record of why a projection must exist: the row
        a site-A reader is entitled to stores site C's id and count."""
        stack = await S.build("X")
        cohort = next(s for s in await S.signals(stack)
                      if s.signal_key == "cohort:dell/r750:SEL_CLEAR")
        assert cohort.evidence["site_failure_counts"][SITES["C"].id] == 30
        assert cohort.evidence["sites_affected"] == 3
        assert "35 of 54 attempts" in cohort.statement
        assert "across 3 sites" in cohort.statement


# ---------------------------------------------------------------------------
# The master oracle
# ---------------------------------------------------------------------------


class TestTwinEstatesAreIndistinguishable:
    @pytest.mark.parametrize("name", SCOPED)
    async def test_a_scoped_reader_is_returned_the_same_bytes(self, name):
        x, y, ids = await _twins()
        subject_x, _ = await E.persona(x, name)
        subject_y, _ = await E.persona(y, name)
        got_x, got_y = await _compare(x, y, subject_x, subject_y, "site_admin", ids)
        assert set(got_x) == set(got_y)
        for surface in got_x:
            assert got_x[surface] == got_y[surface], surface

    async def test_the_tenant_reader_tells_them_apart_on_EVERY_surface(self):
        """NON-VACUITY. If the tenant owner could not distinguish the twins
        on a surface, the scoped equality above would prove nothing there."""
        x, y, ids = await _twins()
        got_x, got_y = await _compare(x, y, E.OWNER, E.OWNER, "tenant_owner", ids)
        assert {"incident", "incidents"} <= set(got_x)
        same = [surface for surface in got_x if got_x[surface] == got_y[surface]]
        assert same == []

    async def test_a_machine_principal_reads_the_same_attention(self, monkeypatch):
        """`/api/attention/` is on MACHINE_SURFACE and is the one read every
        Operational Agent must hold."""
        monkeypatch.setattr(agent_runtime, "SMClient", _FakeSM)
        results = {}
        for variant in ("X", "Y"):
            stack = await S.build(variant)
            agent_id = await _agent(stack, ("A",), name="S4 Machine")
            res = await stack.as_machine(agent_id).get("/api/attention/")
            assert res.status_code == 200, res.text
            body = res.json()
            assert S.hidden_markers(body, ("A",)) == []
            results[variant] = S.canonical(
                _learning_of_attention(body), await S.aliases(stack))
            assert "about 65%" in results[variant]          # it still LEARNS
        assert results["X"] == results["Y"]


# ---------------------------------------------------------------------------
# Order, rank and the limit cut
# ---------------------------------------------------------------------------


def _actions(rows, key="action_type"):
    return [r[key] for r in rows if r.get(key) in ("BMC_RESET", "POWER_CYCLE")]


class TestRowOrderIsNotAChannel:
    """Two same-band states must not be told apart by WHERE a row sits.

    Two channels, both real: the signal repository orders by the EXACT
    confidence; and `OutcomeAggregator.get_metrics` sorts cohorts by their
    tenant attempt total, so detection order -- and therefore `detected_at`,
    `started_at`, the pattern list, the cycle list, what is pushed to a
    Site Manager and what it cites -- ranks cohorts by a hidden total.
    """

    async def test_signals_tenant_order_flips_and_scoped_order_does_not(self):
        order = {}
        for variant in ("X", "Y"):
            stack = await S.build(variant)
            subject, _ = await E.persona(stack, "site_a")
            order[variant] = (
                _actions((await _get(stack, E.OWNER, "/api/learning/signals",
                                     "tenant_owner"))["signals"]),
                _actions((await _get(stack, subject, "/api/learning/signals"))["signals"]),
            )
        assert order["X"][0] == ["POWER_CYCLE", "BMC_RESET"]      # 0.75 > 0.70
        assert order["Y"][0] == ["BMC_RESET", "POWER_CYCLE"]      # 0.80 > 0.75
        assert order["X"][1] == order["Y"][1]

    @pytest.mark.parametrize("path,key", [
        ("/api/outcomes/patterns", "patterns"),
        ("/api/learning/cycles", "cycles"),
    ])
    async def test_detection_order_is_a_rank_by_hidden_total(self, path, key):
        seen = {}
        for variant in ("X", "Y"):
            stack = await S.build(variant)
            names = await S.aliases(stack)
            subject, _ = await E.persona(stack, "site_a")

            def batch_order(body):
                ids = [r.get("pattern_id") for r in body[key]]
                return [names[i] for i in ids
                        if names.get(i, "").startswith("<batch_failure:")
                        and "SEL_CLEAR" not in names[i]]

            seen[variant] = (
                batch_order(await _get(stack, E.OWNER, path, "tenant_owner")),
                batch_order(await _get(stack, subject, path)),
            )
        assert seen["X"][0] != seen["Y"][0], "the fixture lost its order twin"
        assert seen["X"][1] == seen["Y"][1]
        assert len(seen["X"][1]) == 2

    @pytest.mark.parametrize("limit", [1, 2, 3])
    async def test_the_limit_cut_is_made_after_projection(self, limit):
        """`?limit=1` in SQL returns the LAST detected pattern of the pass:
        the cohort with the smallest tenant total."""
        cut = {}
        for variant in ("X", "Y"):
            stack = await S.build(variant)
            names = await S.aliases(stack)
            subject, _ = await E.persona(stack, "site_a")
            body = await _get(stack, subject, "/api/outcomes/patterns", limit=limit)
            assert len(body["patterns"]) == limit
            cut[variant] = [names[p["pattern_id"]] for p in body["patterns"]]
        assert cut["X"] == cut["Y"]

    async def test_sub_second_instants_do_not_carry_the_rank(self):
        """The instants of one pass are microseconds apart IN RANK ORDER. A
        scoped reader is shown one floored instant for all of them."""
        stack = await S.build("X")
        subject, _ = await E.persona(stack, "site_a")
        tenant = await _get(stack, E.OWNER, "/api/outcomes/patterns", "tenant_owner")
        scoped = await _get(stack, subject, "/api/outcomes/patterns")
        assert len({p["detected_at"] for p in tenant["patterns"]}) == 4
        assert len({p["detected_at"] for p in scoped["patterns"]}) == 1
        cycles = await _get(stack, subject, "/api/learning/cycles")
        assert len({c["started_at"] for c in cycles["cycles"]}) == 1
        signals = await _get(stack, subject, "/api/learning/signals")
        assert len({s["last_confirmed_at"] for s in signals["signals"]}) == 1

    def test_a_frozen_copy_is_reordered_at_read_and_never_rewritten(self):
        def stored(bmc, power):
            rows = [
                {"signal_id": "sig-bmc", "statement": "BMC_RESET fails",
                 "confidence": bmc, "scope_type": "cohort", "scope_ref": "dell/r750"},
                {"signal_id": "sig-power", "statement": "POWER_CYCLE fails",
                 "confidence": power, "scope_type": "cohort", "scope_ref": "dell/r750"},
            ]
            return sorted(rows, key=lambda r: -r["confidence"])   # as the evaluator writes

        x, y = stored(0.70, 0.75), stored(0.80, 0.75)
        assert [r["signal_id"] for r in x] != [r["signal_id"] for r in y]
        before = json.dumps(x)
        held = frozenset({SITES["A"].id})
        assert LP.project_frozen_signals(x, held) == LP.project_frozen_signals(y, held)
        assert json.dumps(x) == before
        assert LP.project_frozen_signals(x, None) == x            # tenant: as stored

    def test_citations_are_reordered_inside_their_own_slots(self):
        def cite(desc, conf):
            return str({"fleet_pattern": {"pattern_id": "p", "pattern_type": "batch_failure",
                                          "description": desc, "confidence": conf}})
        bmc_x = cite("BMC_RESET fails at 57% on Dell R750 (8/14)", 0.7)
        bmc_y = cite("BMC_RESET fails at 56% on Dell R750 (9/16)", 0.8)
        power = cite("POWER_CYCLE fails at 60% on Dell R750 (9/15)", 0.75)
        x = ["fan 3 of 5", power, "temp 95%", bmc_x]      # 15 > 14
        y = ["fan 3 of 5", bmc_y, "temp 95%", power]      # 16 > 15
        held = frozenset({SITES["A"].id})
        got_x, got_y = LP.project_citations(x, held), LP.project_citations(y, held)
        assert got_x == got_y
        assert (got_x[0], got_x[2]) == ("fan 3 of 5", "temp 95%")
        assert LP.project_citations(x, None) is x


# ---------------------------------------------------------------------------
# Nothing hidden is named, nothing is counted
# ---------------------------------------------------------------------------


class TestNothingHiddenIsNamedOrCounted:
    @pytest.mark.parametrize("name", SCOPED)
    async def test_no_hidden_site_and_no_count_reaches_a_scoped_reader(self, name):
        stack = await S.build("X")
        incident_id = await S.seed_cited_incident(stack)
        subject, holds = await E.persona(stack, name)
        got = await _surfaces(stack, subject, incident_id=incident_id)
        # `s3-sig-*` fixtures name their site in the row's own scope_ref for
        # readers who hold it; hidden markers are the ones they do not.
        assert S.hidden_markers(got, holds) == []
        for text in _strings(got):
            for shape in COUNT_SHAPES:
                assert not shape.search(text), text
        for path, leaf in S.walk(got):
            if leaf in ("total", "failures", "sites_affected", "model_total"):
                assert path.endswith(f".{leaf}") is False or "withheld" in path, path

    async def test_the_structured_evidence_names_what_it_withheld(self):
        stack = await S.build("X")
        subject, _ = await E.persona(stack, "site_a")
        body = await _get(stack, subject, "/api/learning/signals")
        cohort = next(s for s in body["signals"]
                      if s["signal_key"] == "cohort:dell/r750:SEL_CLEAR")
        assert cohort["evidence"] == {
            "failure_rate": 0.65,
            "site_failure_counts": {SITES["A"].id: 3},
            "projection": "scoped",
            "withheld": ["failures", "sites_affected", "total"],
            "partial": ["site_failure_counts"],
        }
        assert cohort["confidence"] == 1.0
        assert cohort["statement"] == "SEL_CLEAR on Dell R750 fails about 65% of the time."

    async def test_partial_is_marked_whether_or_not_anything_was_dropped(self):
        """A marker that appeared only when a site was dropped would itself
        say that hidden sites exist."""
        stack = await S.build("X")
        everything = frozenset(s.id for s in SITES.values())
        cohort = next(s for s in await S.signals(stack)
                      if s.signal_key == "cohort:dell/r750:SEL_CLEAR")
        assert LP.project_evidence(cohort.evidence, everything)["partial"] == [
            "site_failure_counts"]

    def test_an_unrecognised_evidence_key_is_withheld(self):
        """Naming what may pass (A25.3): tomorrow's key does not leak."""
        got = LP.project_evidence(
            {"failure_rate": 0.5, "sites_breakdown_v2": {"s3-site-c": 9}, "n": 4},
            frozenset({SITES["A"].id}),
        )
        assert set(got) == {"failure_rate", "projection", "withheld", "partial"}
        assert got["withheld"] == ["n", "sites_breakdown_v2"]

    def test_text_that_cannot_be_proven_bounded_is_replaced_whole(self):
        held = frozenset({SITES["A"].id})
        assert LP.reduce_text("novel wording: 41 of 77 tries", held) == LP.WITHHELD_STATEMENT
        assert LP.reduce_text("seen at 9 sites", held) == LP.WITHHELD_STATEMENT
        assert LP.reduce_text("the fleet fails 73% here", held) == LP.WITHHELD_STATEMENT
        assert LP.reduce_text("cohort-knowledge", held) == "cohort-knowledge"
        assert LP.reduce_text("41 of 77 tries", None) == "41 of 77 tries"


# ---------------------------------------------------------------------------
# A23 is retained: the conclusion stays
# ---------------------------------------------------------------------------


class TestTheCohortConclusionSurvives:
    @pytest.mark.parametrize("name", SCOPED)
    async def test_every_scoped_reader_still_learns_what_the_fleet_learned(self, name):
        stack = await S.build("X")
        subject, _ = await E.persona(stack, name)
        body = await _get(stack, subject, "/api/learning/signals")
        cohort = {s["action_type"]: s for s in body["signals"]
                  if s["signal_key"].startswith("cohort:dell/r750:")}
        assert set(cohort) == {"SEL_CLEAR", "BMC_RESET", "POWER_CYCLE"}
        assert cohort["SEL_CLEAR"]["evidence"]["failure_rate"] == 0.65
        assert cohort["BMC_RESET"]["evidence"]["failure_rate"] == 0.55
        assert cohort["BMC_RESET"]["confidence"] == 0.75
        assert (cohort["SEL_CLEAR"]["vendor"], cohort["SEL_CLEAR"]["model"]) == ("Dell", "R750")
        assert "about 55%" in cohort["BMC_RESET"]["statement"]

    async def test_a_site_reader_keeps_its_OWN_sites_facts(self):
        stack = await S.build("X")
        subject, _ = await E.persona(stack, "site_a")
        body = await _get(stack, subject, "/api/learning/signals")
        own = next(s for s in body["signals"]
                   if s["signal_key"] == f"site:{SITES['A'].id}:SEL_CLEAR")
        assert own["evidence"]["failures_at_site"] == 3
        assert own["evidence"]["site_failure_counts"] == {SITES["A"].id: 3}
        patterns = await _get(stack, subject, "/api/outcomes/patterns")
        cross = next(p for p in patterns["patterns"]
                     if p["pattern_type"] == "cross_site_batch")
        assert cross["affected_scope"] == {
            "action_type": "SEL_CLEAR", "vendor": "Dell", "model": "R750",
            "sites": SITES["A"].id,                       # CSV string kept a string
        }
        assert cross["description"] == (
            "SEL_CLEAR fails at about 65% on Dell R750, at more than one site")

    async def test_A23_1_row_rules_are_unchanged(self):
        """A pattern whose named sites are all hidden is absent; a
        site-scoped signal follows its site."""
        stack = await S.build("X")
        subject, _ = await E.persona(stack, "site_d")
        patterns = await _get(stack, subject, "/api/outcomes/patterns")
        assert {p["pattern_type"] for p in patterns["patterns"]} == {"batch_failure"}
        signals = await _get(stack, subject, "/api/learning/signals")
        assert [s for s in signals["signals"] if s["scope_type"] == "site"] == []

    async def test_prior_learning_is_a_fleet_view_fact_under_incident_view(self):
        """`a_plus_narrow_c` holds site C for `incident.view` ONLY. It may
        open an incident at C; the learned evidence there is bounded by its
        `fleet.view` reach, which is site A."""
        stack = await S.build("X")
        incident_id = await S.seed_cited_incident(stack, "C")
        subject, _ = await E.persona(stack, "a_plus_narrow_c")
        body = await _get(stack, subject, f"/api/incidents/{incident_id}")
        learned = body["prior_learning"]
        assert learned, "the cohort conclusion is tenant knowledge and must show"
        assert all(s["scope_type"] == "cohort" for s in learned)
        for signal in learned:
            assert SITES["C"].id not in json.dumps(signal["evidence"])
            assert signal["evidence"].get("projection", "scoped") == "scoped"
            assert "total" not in signal["evidence"]


# ---------------------------------------------------------------------------
# The tenant-wide reader is unchanged
# ---------------------------------------------------------------------------


class TestTheTenantReaderIsByteIdentical:
    async def test_signals_patterns_and_cycles_are_what_is_stored(self):
        stack = await S.build("X")
        body = await _get(stack, E.OWNER, "/api/learning/signals", "tenant_owner")
        stored = {s.signal_key: s for s in await S.signals(stack)}
        assert [s["confidence"] for s in body["signals"]] == sorted(
            (s.confidence for s in stored.values()), reverse=True)
        for got in body["signals"]:
            row = stored[got["signal_key"]]
            assert got["evidence"] == (row.evidence or {})
            assert got["statement"] == row.statement
            assert got["confidence"] == row.confidence

        patterns = await _get(stack, E.OWNER, "/api/outcomes/patterns", "tenant_owner")
        by_id = {p.id: p for p in await S.patterns(stack)}
        assert [p["pattern_id"] for p in patterns["patterns"]] == [
            p.id for p in sorted(by_id.values(), key=lambda p: p.detected_at, reverse=True)]
        for got in patterns["patterns"]:
            row = by_id[got["pattern_id"]]
            assert (got["description"], got["evidence"], got["affected_scope"],
                    got["confidence"]) == (
                row.description, row.evidence, row.affected_scope, row.confidence)

        cycles = await _get(stack, E.OWNER, "/api/learning/cycles", "tenant_owner")
        assert {c["sites_distributed"] for c in cycles["cycles"]} == {3}
        assert {c["devices_applied"] for c in cycles["cycles"]} == {31}
        assert "projection" not in json.dumps([body, patterns, cycles])

    def test_a_tenant_wide_projection_returns_the_stored_object(self):
        rows, evidence, entries = [object()], {"total": 9}, ["x"]
        assert LP.project_signals(rows, None) == rows
        assert LP.project_patterns(rows, None) == rows
        assert LP.project_evidence(evidence, None) is evidence
        assert LP.project_citations(entries, None) is entries
        payload = {"sites_distributed": 4}
        assert LP.project_cycles([payload], None) == [payload]


# ---------------------------------------------------------------------------
# Ratification A: the distribution payload
# ---------------------------------------------------------------------------


async def _detected(stack) -> list[FleetPattern]:
    """The stored patterns as the distributor holds them, in its order."""
    return [
        FleetPattern(
            pattern_id=p.id, pattern_type=p.pattern_type, description=p.description,
            affected_scope=p.affected_scope, confidence=p.confidence,
            detected_at=p.detected_at.timestamp(), evidence=p.evidence,
        )
        for p in await S.patterns(stack)
    ]


class TestTheDistributionPayload:
    async def test_a_site_manager_is_a_reader_that_holds_exactly_itself(self):
        pushed = {}
        for variant in ("X", "Y"):
            stack = await S.build(variant)
            raw = site_payload(await _detected(stack), SITES["A"].id)
            for site in ("B", "C", "D"):
                assert SITES[site].id not in raw
            for shape in COUNT_SHAPES:
                assert not shape.search(raw), raw
            payload = json.loads(raw)
            for p in payload:
                assert {"pattern_id", "pattern_type", "description", "affected_scope",
                        "confidence", "evidence", "detected_at"} == set(p)   # what the SM reads
            pushed[variant] = S.canonical(payload, await S.aliases(stack))
        assert pushed["X"] == pushed["Y"]

    async def test_a_site_that_is_not_failing_yet_is_still_told(self):
        """R-C2 is why the loop exists. Site D holds the cohort and is not
        named by the cross-site pattern: it is told the bounded conclusion."""
        stack = await S.build("X")
        payload = json.loads(site_payload(await _detected(stack), SITES["D"].id))
        cross = next(p for p in payload if p["pattern_type"] == "cross_site_batch")
        assert cross["affected_scope"]["sites"] == ""
        assert cross["evidence"]["failure_rate"] == 0.65
        assert "site_failure_counts" not in cross["evidence"]
        assert "more than one site" in cross["description"]

    async def test_the_distributor_pushes_the_projected_payload(self):
        """`distribute_patterns` builds its push body with `site_payload`
        and nothing else."""
        source = (pathlib.Path(harkeniq_cc.__file__).parent
                  / "knowledge_distributor.py").read_text()
        tree = ast.parse(source)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.AsyncFunctionDef) and n.name == "distribute_patterns")
        calls = {getattr(c.func, "id", getattr(c.func, "attr", ""))
                 for c in ast.walk(fn) if isinstance(c, ast.Call)}
        assert "site_payload" in calls
        assert "dumps" not in calls, "a second, unprojected serialisation"


# ---------------------------------------------------------------------------
# Ratification B: evidence_cited
# ---------------------------------------------------------------------------


class TestEvidenceCited:
    async def test_pattern_citations_are_bounded_and_telemetry_is_not(self):
        stack = await S.build("X")
        incident_id = await S.seed_cited_incident(stack)
        subject, _ = await E.persona(stack, "site_a")
        scoped = (await _get(stack, subject, f"/api/incidents/{incident_id}"))["diagnosis"]
        tenant = (await _get(stack, E.OWNER, f"/api/incidents/{incident_id}",
                             "tenant_owner"))["diagnosis"]
        assert tenant["evidence_cited"][0] == S.INCIDENT_TELEMETRY
        assert any("(35/54)" in c for c in tenant["evidence_cited"])
        assert any("across 3 sites" in c for c in tenant["evidence_cited"])

        assert scoped["evidence_cited"][0] == S.INCIDENT_TELEMETRY     # untouched
        assert len(scoped["evidence_cited"]) == len(tenant["evidence_cited"])
        cited = scoped["evidence_cited"][1:]
        assert all("fleet_pattern" in c for c in cited)
        for text in cited:
            for shape in COUNT_SHAPES:
                assert not shape.search(text), text
            assert "'confidence': 0.7," not in text and "'confidence': 0.7}" not in text
        assert any("about 65%" in c for c in cited)                    # the conclusion

    def test_a_citation_outside_the_grammar_fails_closed(self):
        held = frozenset({SITES["A"].id})
        odd = str({"fleet_pattern": {"description": "12 of 90 units at 4 sites"}})
        assert LP.project_citation(odd, held) == LP.WITHHELD_CITATION
        assert LP.project_citation("12 of 90 fans", held) == "12 of 90 fans"

    def test_no_diagnosis_is_shaped_without_a_reader(self):
        with pytest.raises(TypeError):
            incidents_api._diagnosis({"provider": "llm"}, None)
        with pytest.raises(TypeError):
            incidents_api._diagnosis({"provider": "llm"}, frozenset())


# ---------------------------------------------------------------------------
# Ratification C: learning cycles
# ---------------------------------------------------------------------------


class TestLearningCycles:
    async def test_a_scoped_reader_is_not_told_the_size_of_the_estate(self):
        stack = await S.build("X")
        subject, _ = await E.persona(stack, "site_a")
        body = await _get(stack, subject, "/api/learning/cycles")
        assert len(body["cycles"]) == 4
        for cycle in body["cycles"]:
            assert cycle["sites_distributed"] is None
            assert cycle["devices_applied"] is None
            assert cycle["withheld"] == ["sites_distributed", "devices_applied"]
            assert "total" not in cycle["outcomes_before"]
            assert cycle["outcomes_before"]["withheld"] == ["total"]
            assert cycle["pattern_type"] and cycle["status"]            # still a cycle

    def test_improvement_is_banded(self):
        held = frozenset({SITES["A"].id})
        a = LP.project_cycle({"improvement_pct": 11.4}, held)["improvement_pct"]
        b = LP.project_cycle({"improvement_pct": 9.2}, held)["improvement_pct"]
        assert a == b == 10.0
        assert LP.project_cycle({"improvement_pct": -11.4}, held)["improvement_pct"] == -10.0
        assert LP.project_cycle({"improvement_pct": None}, held)["improvement_pct"] is None

    def test_the_contract_describes_runtime_truth(self):
        assert ROUTE_CONTRACT[("GET", "/api/learning/cycles")] == (
            "fleet.view", READ_SCOPED, False)


# ---------------------------------------------------------------------------
# Frozen copies on a proposal
# ---------------------------------------------------------------------------


class _FakeSM:
    def __init__(self, *_a, **_kw):
        pass

    async def dispatch_action(self, endpoint, token, **kw):
        return {"accepted": True, "directive_id": "dir-s4", "reason": ""}


async def _agent(stack, keys, *, name) -> str:
    from harkeniq_cc.db.models import CCOperationalAgent

    async with stack.as_person().client() as client:
        res = await client.post("/api/operational-agents/", json={
            "name": name,
            "scopes": [{"scope_type": "site", "scope_ref": stack.site(k)} for k in keys],
            "capabilities": [{"kind": "action_class", "capability_ref": "SEL_CLEAR"}],
        })
    assert res.status_code == 201, res.text
    agent_id = res.json()["id"]
    async with stack.sessionmaker() as session:
        agent = await session.get(CCOperationalAgent, agent_id)
        agent.status = "active"
        agent.activated_version = agent.version
        await session.commit()
    return agent_id


def _frozen_of(proposal: dict) -> list:
    body = proposal.get("proposal", proposal)
    return (body.get("evidence") or {}).get("learned_signals") or []


class TestAProposalsFrozenSignals:
    async def test_the_recorded_signals_are_bounded_for_a_scoped_reader(self, monkeypatch):
        monkeypatch.setattr(agent_runtime, "SMClient", _FakeSM)
        monkeypatch.setattr(approvals_api, "SMClient", _FakeSM)
        seen = {}
        for variant in ("X", "Y"):
            stack = await S.build(variant)
            agent_id = await _agent(stack, ("A",), name="S4 Runtime")
            await E.seed_incident(stack, "A")
            stats = await agent_runtime.run_once(stack.state, stack.tenant)
            assert stats["proposed"] == 1, stats
            subject, _ = await E.persona(stack, "site_a")
            path = f"/api/operational-agents/{agent_id}/proposals"
            tenant = _frozen_of((await _get(stack, E.OWNER, path, "tenant_owner"))["proposals"][0])
            scoped = _frozen_of((await _get(stack, subject, path))["proposals"][0])
            queue = await _get(stack, subject, "/api/approvals/")

            assert any("of 5" in s["statement"] and "attempts" in s["statement"]
                       for s in tenant), "the WRITE path is unchanged: exact text is recorded"
            assert scoped, "the cohort conclusion is still shown"
            for text in _strings([scoped, queue]):
                for shape in COUNT_SHAPES:
                    assert not shape.search(text), text
            assert S.hidden_markers(scoped, ("A",)) == []
            seen[variant] = (
                json.dumps([{k: v for k, v in s.items() if k != "signal_id"} for s in scoped]),
                json.dumps([s["statement"] for s in tenant]),
            )
        assert seen["X"][0] == seen["Y"][0]
        assert seen["X"][1] != seen["Y"][1]


# ---------------------------------------------------------------------------
# Structure: the two easy ways back
# ---------------------------------------------------------------------------

PACKAGE = pathlib.Path(harkeniq_cc.__file__).parent

#: INTERNAL DECISION paths (A30.28): they read rank, band, driver and score
#: off each attention item and never return the payload.
INTERNAL_ATTENTION = {
    ("agent_runtime.py", "evaluate_agents"),
    ("api/operational_agents.py", "dry_run_agent"),
    ("api/operational_agents.py", "submit_proposal"),
}


def _attention_calls():
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) \
                        and getattr(node.func, "id", "") == "load_attention":
                    learning = {k.arg: k.value for k in node.keywords}.get("learning")
                    yield str(path.relative_to(PACKAGE)), fn.name, learning


class TestLearnedKnowledgeCannotBeReadWithoutAReader:
    def test_every_attention_caller_names_its_reader(self):
        calls = list(_attention_calls())
        assert len(calls) >= 4, calls
        assert [c[:2] for c in calls if c[2] is None] == []

    def test_only_the_decision_paths_read_unprojected_learning(self):
        unprojected = {
            (module, fn) for module, fn, learning in _attention_calls()
            if isinstance(learning, ast.Constant) and learning.value is None
        }
        assert unprojected == INTERNAL_ATTENTION

    def test_the_view_is_a_type_and_only_the_scope_builds_it(self):
        for wrong in (None, frozenset(), set(), [], "tenant", empty_scope(E.TENANT)):
            with pytest.raises(TypeError):
                require_learning_view(wrong)
        view = learning_view(empty_scope(E.TENANT))
        assert isinstance(view, LearningView) and view.sites == frozenset()
        assert require_learning_view(view) is view

    def test_the_route_local_narrowing_is_gone(self):
        for name in ("_narrow_sites", "_pattern_visible", "_SITE_KEYS", "_visible_sites"):
            assert not hasattr(outcomes_api, name), name

    def test_every_number_in_a_statement_comes_from_its_evidence(self):
        """A scoped statement is `render_statement` over PROJECTED evidence.
        That bounds the text only while no number enters another way."""
        for kind in ("batch_failure", "cross_site_batch", "reliability", "anomaly"):
            for site in ("cohort", "site"):
                text = render_statement(
                    kind, "SEL_CLEAR", "Dell", "R750", {}, site, "this site")
                assert not re.search(r"\d", text.replace("R750", "")), text
        evidence = {"failure_rate": 0.65, "fleet_failure_rate": 0.2, "trend": 0.3}
        for kind in ("batch_failure", "cross_site_batch", "reliability", "anomaly"):
            text = render_statement(kind, "SEL_CLEAR", "Dell", "R750", evidence,
                                    "cohort", "", approximate=True)
            numbers = set(re.findall(r"\d+", text.replace("R750", "")))
            assert numbers <= {"65", "20", "30"}, text
            assert "about" in text

    def test_bands_restated_independently(self):
        for value, want in [(0.648, 0.65), (0.638, 0.65), (0.571, 0.55), (0.5625, 0.55),
                            (0.0, 0.0), (1.0, 1.0), (0.024, 0.0), (0.025, 0.05),
                            (-0.31, -0.3)]:
            assert LP.band_rate(value) == want, value
        for value, want in [(0.7, 0.75), (0.8, 0.75), (0.75, 0.75), (1.0, 1.0),
                            (0.9, 1.0), (0.15, 0.25), (0.01, 0.25), (0.0, 0.0),
                            (0.6, 0.5), (None, 0.0)]:
            assert LP.band_confidence(value) == want, value

    def test_a_band_never_encodes_the_count_it_replaces(self):
        """Every attempt total from 1 to 200 maps to one of FOUR confidence
        values, and each value is shared by many totals."""
        bands = {}
        for total in range(1, 201):
            bands.setdefault(LP.band_confidence(min(1.0, total / 20)), []).append(total)
        assert set(bands) == {0.25, 0.5, 0.75, 1.0}
        assert min(len(v) for v in bands.values()) >= 5
