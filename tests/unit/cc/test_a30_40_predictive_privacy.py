"""A30.40: predictive / privacy projection hardening.

Every claim of the amendment, on the S3 production stack over ONE poisoned
predictive estate (`tests/unit/cc/a30_40_estate.py`): the real `get_scope`,
persisted grants, STRICT.

* D-P2 / D-P3 / D-P5 -- a scoped person's predictive answer and Attention
  are composed from the outcome rows their CURRENT canonical reach reads:
  deletion equivalence against a TENANT reader of an independently reduced
  estate, with a tenant-wide control that must move;
* D-P4 -- `outcomes_considered` is the reader's own count, 0 with no reach;
* hidden rows -- failures, successes, model cohorts, one member added and
  removed, edge rates, count and rate sentinels, the moved device, repeated
  reads, band and rank thresholds -- change nothing a scoped person reads;
* D-P6 -- "in your current view" for a scoped reader, tenant wording kept;
* D-P7 -- every machine, tenant-wide included, reads bounded learning;
* the tenant-wide view and the three internal decision paths are
  byte-identical to a golden recorded from unmodified code;
* D-P10 -- the reader's own 50k window is F-E2-4's, pinned.
"""

from __future__ import annotations

import ast
import json
import pathlib
import re
from datetime import timedelta

import pytest
from sqlalchemy import insert

import harkeniq_cc
from harkeniq_cc.db.models import CCOutcomeHistory
from harkeniq_cc.governance import (
    HumanAttentionSelection,
    LearningView,
    MachineAttentionSelection,
    human_attention_selection,
    learning_view,
    load_attention,
    load_human_attention,
    machine_learning_view,
    require_human_attention_selection,
)
from harkeniq_cc.scope import ReadReach, empty_scope

from tests.unit.cc import a30_40_estate as P
from tests.unit.cc import b2_2_estate as E


GOLDEN = pathlib.Path(__file__).parent / "golden" / "a30_40_tenant_wide_predictive.json"
SRC = pathlib.Path(harkeniq_cc.__file__).parent


def _golden() -> dict:
    return json.loads(GOLDEN.read_text())


def _rows(payload: dict) -> dict:
    return {r["agent_id"]: r for r in payload["risks"]}


def _items(payload: dict) -> dict:
    return {i["agent_id"]: i for i in payload["items"]}


def _hidden_site(name: str) -> str:
    """A site whose site-owned rows this persona does not read."""
    return next(s for s in ("C", "B", "D") if s not in P.held_sites(name))


async def _both(stack, name: str) -> tuple[dict, dict]:
    """Every predictive and Attention answer this persona reads, by query."""
    pred = {json.dumps(q, sort_keys=True): await P.predictive(stack, name, **P.query(stack, q))
            for q in P.PREDICTIVE_QUERIES}
    attn = {json.dumps(q, sort_keys=True): await P.attention(stack, name, **P.query(stack, q))
            for q in P.ATTENTION_QUERIES}
    return pred, attn


def _oracle_count(name: str, variant: str = "heavy") -> int:
    """The outcome rows `visible()` keeps for this persona -- restated."""
    keep = P.visible(name, variant)
    base = sum(ok + bad for i, (*_r, ok, bad) in enumerate(E.OUTCOMES) if i in keep.base.outcomes)
    extra = sum(ok + bad for key, *_r, ok, bad in P.extra_outcomes(variant) if key in keep.outcomes)
    return base + extra


# ---------------------------------------------------------------------------
# The tenant-wide view and the internal decision paths: byte-identical
# ---------------------------------------------------------------------------


class TestTheTenantWideViewIsUnchanged:
    async def test_the_tenant_owner_reads_main_heavy(self):
        full = await P.build(P.Keep.everything("heavy"))
        assert await P.tenant_wide_payloads(full) == _golden()["tenant_wide"]

    async def test_the_tenant_owner_reads_main_light(self):
        full = await P.build(P.Keep.everything("light"))
        assert await P.tenant_wide_payloads(full) == _golden()["tenant_wide_light"]

    async def test_the_internal_decision_paths_read_main(self):
        """The evaluator, the dry-run's reasoning and the ingress re-derivation
        still decide over the whole tenant: byte-identical (D-P5; proposal-
        budget ordering stays OPEN)."""
        full = await P.build(P.Keep.everything("heavy"))
        assert await E.internal_payloads(full) == _golden()["internal"]

    def test_the_golden_carries_the_poison_it_proves_unchanged(self):
        """NON-VACUITY: the tenant reader DOES read the hidden totals."""
        golden = _golden()
        assert golden["recorded_at_commit"].startswith("96557b6")
        top = golden["tenant_wide"]["predictive"]["{}"]
        assert top["outcomes_considered"] == P.SENTINEL_TOTAL
        rows = {r["agent_id"]: r for r in top["risks"]}
        assert rows["b22-a3"]["factors"]["cohort_failure_rate"] == P.SENTINEL_RATE
        assert rows["b22-a6"]["factors"]["cohort_failure_rate"] == 1.0
        assert rows["b22-a7"]["factors"]["cohort_failure_rate"] == 0.0
        assert rows["b22-a8"]["factors"]["cohort_failure_rate"] == 0.3333
        assert rows["b22-mv"]["sample_count"] == 7
        light = golden["tenant_wide_light"]["predictive"]["{}"]
        a3_light = {r["agent_id"]: r for r in light["risks"]}["b22-a3"]
        assert (rows["b22-a3"]["band"], a3_light["band"]) == ("high", "medium")


# ---------------------------------------------------------------------------
# Deletion equivalence: scoped(full) == tenant(independently reduced)
# ---------------------------------------------------------------------------


class TestDeletionEquivalence:
    @pytest.mark.parametrize("name", P.READERS)
    async def test_predictive_equals_a_tenant_reader_of_the_reduced_estate(self, name):
        full = await P.build()
        reduced = await P.build(P.visible(name))
        for q in P.PREDICTIVE_QUERIES:
            scoped = await P.predictive(full, name, **P.query(full, q))
            tenant = await P.predictive(reduced, "h-owner", **P.query(reduced, q))
            same_reader = await P.predictive(reduced, name, **P.query(reduced, q))
            assert scoped == tenant, (name, q)
            assert scoped == same_reader, (name, q)

    @pytest.mark.parametrize("name", P.READERS)
    async def test_attention_equals_a_tenant_reader_of_the_reduced_estate(self, name):
        full = await P.build()
        reduced = await P.build(P.visible(name))
        for q in P.ATTENTION_QUERIES:
            scoped = await P.attention(full, name, **P.query(full, q))
            tenant = await P.attention(reduced, "h-owner", **P.query(reduced, q))
            same_reader = await P.attention(reduced, name, **P.query(reduced, q))
            assert P.comparable(scoped) == P.comparable(tenant), (name, q)
            assert scoped == same_reader, (name, q)

    @pytest.mark.parametrize("name", P.READERS)
    async def test_the_tenant_wide_control_moves(self, name):
        """NON-VACUITY: deleting what this persona may not read changes the
        tenant owner's answer -- so the equalities above mean something."""
        full = await P.build()
        reduced = await P.build(P.visible(name))
        assert await P.predictive(full, "h-owner") != await P.predictive(reduced, "h-owner")
        assert await P.attention(full, "h-owner") != await P.attention(reduced, "h-owner")

    @pytest.mark.parametrize("name", P.READERS)
    async def test_outcomes_considered_is_the_readers_own_count(self, name):
        full = await P.build()
        body = await P.predictive(full, name)
        assert body["outcomes_considered"] == _oracle_count(name), name
        assert body["outcomes_considered"] < P.SENTINEL_TOTAL


# ---------------------------------------------------------------------------
# Zero effective reach: zero predictive evidence
# ---------------------------------------------------------------------------


class TestZeroReach:
    @pytest.mark.parametrize("name", P.ZERO_REACH)
    async def test_no_risk_no_count_no_attention(self, name):
        full = await P.build()
        for q in P.PREDICTIVE_QUERIES:
            body = await P.predictive(full, name, **P.query(full, q))
            assert (body["risks"], body["devices_scored"], body["outcomes_considered"]) == (
                [], 0, 0), (name, q)
        for q in P.ATTENTION_QUERIES:
            body = await P.attention(full, name, **P.query(full, q))
            assert body["items"] == [] and body["sites"] == [] and body["returned"] == 0
            assert set(body["summary"].values()) == {0}, body["summary"]

    async def test_the_control_reads_the_tenant_total(self):
        full = await P.build()
        assert (await P.predictive(full, "h-owner"))["outcomes_considered"] == P.SENTINEL_TOTAL


# ---------------------------------------------------------------------------
# Hidden rows change nothing a scoped person reads
# ---------------------------------------------------------------------------


class TestHiddenRowsMoveNothing:
    @pytest.mark.parametrize("name", P.READERS)
    async def test_one_hidden_member_added_and_removed_and_read_again(self, name):
        """Repeated-query differencing: the scoped answer is byte-identical
        across a hidden failure, a hidden success and their removal."""
        full = await P.build()
        site = _hidden_site(name)
        baseline = await _both(full, name)
        tenant = [await P.predictive(full, "h-owner")]
        for step, (outcome, n) in enumerate((("FAILURE", 1), ("SUCCESS", 2))):
            await P.add_hidden(full, outcome, n, site=site)
            assert await _both(full, name) == baseline, (name, outcome)
            tenant.append(await P.predictive(full, "h-owner"))
        await P.remove_hidden(full, 1)
        assert await _both(full, name) == baseline, name
        tenant.append(await P.predictive(full, "h-owner"))
        counts = [t["outcomes_considered"] for t in tenant]
        assert counts == [P.SENTINEL_TOTAL, P.SENTINEL_TOTAL + 1,
                          P.SENTINEL_TOTAL + 2, P.SENTINEL_TOTAL + 1], counts
        rates = [_rows(t)["b22-a3"]["factors"]["cohort_failure_rate"] for t in tenant]
        assert len(set(rates)) > 1, rates

    @pytest.mark.parametrize("name", ["h-site-a", "h-org-ab", "h-class-server", "h-mixed-incident"])
    async def test_a_model_seen_only_at_a_hidden_site_stays_unscored(self, name):
        """Edge rates 100%, 0% and 1/3 -- each a cohort that exists only in
        hidden rows -- must read as no evidence at all."""
        full = await P.build()
        rows = _rows(await P.predictive(full, name))
        items = _items(await P.attention(full, name))
        for key in ("a6", "a7", "a8"):
            agent = P.agent(full, key)
            assert rows[agent]["band"] == "insufficient_data", (name, key, rows[agent])
            assert rows[agent]["factors"] == {"basis": "insufficient_data", "min_samples": 5}
            assert items[agent]["confidence"]["basis"] == "insufficient_data"
            assert items[agent]["attention_driver"] == "insufficient_evidence"
            assert items[agent]["recommended_next"]["capability"] == "collect_evidence"

    async def test_a_reader_who_holds_the_site_reads_those_cohorts(self):
        """The rule is reach, not a blanket: fleet.view at C reads C's rows."""
        full = await P.build()
        rows = _rows(await P.predictive(full, "h-mixed"))
        rates = {key: rows[P.agent(full, key)]["factors"].get("cohort_failure_rate")
                 for key in ("a6", "a7", "a8")}
        assert rates == {"a6": 1.0, "a7": 0.0, "a8": 0.3333}

    @pytest.mark.parametrize("name", ["h-site-a", "h-org-ab", "h-class-server", "h-mixed-incident"])
    async def test_hidden_rows_across_band_and_rank_thresholds(self, name):
        """LIGHT and HEAVY differ only at the hidden site. The tenant control's
        a3 crosses the medium/high band there; a scoped reader's does not move."""
        heavy = await P.build(P.Keep.everything("heavy"))
        light = await P.build(P.Keep.everything("light"))
        assert await _both(heavy, name) == await _both(light, name), name
        a3 = P.agent(heavy, "a3")
        tenant_heavy = _items(await P.attention(heavy, "h-owner"))[a3]
        tenant_light = _items(await P.attention(light, "h-owner"))[a3]
        assert (tenant_heavy["band"], tenant_light["band"]) == ("high", "medium")
        assert tenant_heavy["rank"] != tenant_light["rank"]

    @pytest.mark.parametrize("name", ["h-site-a", "h-device-mv", "h-class-server", "h-org-ab"])
    async def test_a_moved_devices_history_at_a_site_not_held_never_counts(self, name):
        full = await P.build()
        mv = P.agent(full, "mv")
        row = _rows(await P.predictive(full, name))[mv]
        item = _items(await P.attention(full, name))[mv]
        assert row["sample_count"] == 1 and row["factors"]["basis"] == "cohort_prior", row
        assert item["confidence"]["sample_count"] == 1
        assert not any("7 outcomes" in r for r in item["reasons"]), item["reasons"]
        owner = _rows(await P.predictive(full, "h-owner"))[mv]
        assert owner["sample_count"] == 7 and owner["factors"]["basis"] == "device_history"

    @pytest.mark.parametrize("name", [n for n in P.READERS if "C" not in P.held_sites(n)])
    async def test_no_count_or_rate_sentinel_reaches_a_reader_who_does_not_hold_c(self, name):
        full = await P.build()
        pred, attn = await _both(full, name)
        percent = f"{round(P.SENTINEL_RATE * 100)}%"
        for path, leaf, is_key in P.walk({"predictive": pred, "attention": attn}):
            if is_key:
                continue
            assert leaf != P.SENTINEL_TOTAL and leaf != P.SENTINEL_RATE, (name, path)
            if isinstance(leaf, str):
                assert str(P.SENTINEL_TOTAL) not in leaf and percent not in leaf, (name, path)
        control = json.dumps(await _both(full, "h-owner"))
        assert str(P.SENTINEL_TOTAL) in control and percent in control

    async def test_a_device_reader_is_scored_on_its_own_rows_only(self):
        """D-P9: ONE cohort function. A device-only reach's cohort is that
        device's own rows -- exactly what a tenant reader computes over an
        estate holding nothing else."""
        full = await P.build()
        a2 = P.agent(full, "a2")
        row = _rows(await P.predictive(full, "h-device-a2"))[a2]
        assert row["factors"] == {"basis": "cohort_prior", "cohort_failure_rate": 1.0,
                                  "health_bump": 0.1, "health": "Warning"}
        assert (await P.predictive(full, "h-device-a2"))["outcomes_considered"] == 2

    async def test_a_grant_withholding_fleet_view_contributes_nothing(self):
        """incident.view at C is not fleet.view at C: C's rows stay out."""
        full = await P.build()
        assert await P.predictive(full, "h-mixed-incident") == await P.predictive(full, "h-site-a")


# ---------------------------------------------------------------------------
# D-P6: the wording
# ---------------------------------------------------------------------------


class TestWording:
    @pytest.mark.parametrize("name", P.READERS)
    async def test_a_scoped_reader_is_told_it_is_their_current_view(self, name):
        full = await P.build()
        body = await P.attention(full, name)
        assert body["items"], name
        for item in body["items"]:
            explanation = item["confidence"]["explanation"]
            assert explanation in P.EXPLANATIONS, explanation
            assert "across the fleet" not in explanation
            predictive = [r for r in item["reasons"]
                          if "outcome" in r or "peers" in r]
            assert predictive and all(P.SCOPED_PHRASE in r for r in predictive), item["reasons"]
            for reason in item["reasons"]:
                if reason.startswith(("Learned for ", "Fleet-wide failure rate")):
                    assert P.SCOPED_PHRASE not in reason, reason

    async def test_the_tenant_reader_keeps_its_words(self):
        full = await P.build()
        text = json.dumps(await P.attention(full, "h-owner"))
        assert P.SCOPED_PHRASE not in text
        assert "across the fleet" in text


# ---------------------------------------------------------------------------
# D-P7: every machine reads bounded learning
# ---------------------------------------------------------------------------

COUNT_TEXT = re.compile(r"\(\s*\d+\s*/\s*\d+\s*\)|\b\d+ of \d+ attempts\b")
GRID = {0.0, 0.25, 0.5, 0.75, 1.0}


def _learning_leaves(item: dict):
    for entry in item.get("prior_learning", []):
        yield entry["statement"]["text"], entry["confidence"]
    for entry in item.get("fleet_patterns", []):
        yield entry["description"]["text"], entry["confidence"]


class TestMachinesReadBoundedLearning:
    async def test_the_stored_learning_carries_counts(self):
        """NON-VACUITY: what is stored is count-bearing and off the grid."""
        stored = next(p for p in E.PATTERNS if p[0] == "xsite")
        assert COUNT_TEXT.search(stored[3]) and stored[4] not in GRID
        assert next(s for s in E.SIGNALS if s[0] == "cohort")[4] not in GRID

    @pytest.mark.parametrize("name", ["m-tenant", "m-site-a", "m-org-ab"])
    async def test_attention_learning_is_bounded_for_every_machine(self, name):
        full = await P.build()
        body = await E.read(full, name)
        seen = 0
        for item in body["items"]:
            for text, confidence in _learning_leaves(item):
                seen += 1
                assert not COUNT_TEXT.search(text), (name, text)
                assert E.HIDDEN_COUNT not in text, (name, text)
                assert confidence in GRID, (name, confidence)
        assert seen, name

    async def test_incident_prior_learning_is_bounded_for_the_tenant_wide_machine(self):
        full = await P.build()
        await E.enter(full, "m-tenant")
        async with full.client() as client:
            res = await client.get(f"/api/incidents/{E.iid(full, 'a1-disk')}")
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["prior_learning"], body
        for entry in body["prior_learning"]:
            assert entry["confidence"] in GRID, entry
            assert not COUNT_TEXT.search(entry["statement"]["text"]), entry
        # A30.29's generated-content gate is a different rule and keeps the
        # reader's own view: the tenant-wide machine still reads the block.
        assert body["diagnosis"]["generated"]["withheld"] is False

    async def test_a_scoped_view_comes_back_unchanged_and_a_tenant_view_holds_every_site(self):
        full = await P.build()
        scoped = LearningView(sites=frozenset({full.site("A")}))
        async with full.sessionmaker() as session:
            assert await machine_learning_view(session, full.tenant, scoped) is scoped
            tenant = await machine_learning_view(session, full.tenant, LearningView(sites=None))
        assert tenant.sites == frozenset(full.site(s) for s in E.S3.ALL)

    async def test_a_person_keeps_the_identity_view(self):
        """D-P7 is about machines: the tenant owner still reads what is stored."""
        full = await P.build()
        text = json.dumps(await P.attention(full, "h-owner"))
        assert f"({E.HIDDEN_COUNT}/7800)" in text


# ---------------------------------------------------------------------------
# Structure: the ways back
# ---------------------------------------------------------------------------


def _tree(rel: str) -> ast.AST:
    return ast.parse((SRC / rel).read_text())


def _called(node) -> set[str]:
    return {getattr(n.func, "id", getattr(n.func, "attr", ""))
            for n in ast.walk(node) if isinstance(n, ast.Call)}


def _functions_calling(name: str) -> set[tuple[str, str]]:
    out = set()
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and name in _called(fn):
                out.add((str(path.relative_to(SRC)), fn.name))
    return out


class TestStructure:
    def test_a_person_s_attention_comes_through_their_selection(self):
        assert _functions_calling("load_human_attention") == {("api/attention.py", "attention")}
        assert _functions_calling("HumanAttentionSelection") == {
            ("governance.py", "human_attention_selection")}
        route = next(fn for fn in ast.walk(_tree("api/attention.py"))
                     if isinstance(fn, ast.AsyncFunctionDef) and fn.name == "attention")
        assert {"human_attention_selection", "load_human_attention"} <= _called(route)
        assert "load_attention" not in _called(route)

    def test_the_selection_is_a_type_and_refuses_anything_else(self):
        for wrong in (None, {}, ReadReach(tenant_id="t", permissions=("fleet.view",)),
                      learning_view(empty_scope(E.S3.TENANT))):
            with pytest.raises(TypeError):
                require_human_attention_selection(wrong)
        selection = human_attention_selection(empty_scope(E.S3.TENANT))
        assert require_human_attention_selection(selection) is selection
        assert HumanAttentionSelection.__dataclass_params__.frozen
        assert not issubclass(HumanAttentionSelection, MachineAttentionSelection)

    async def test_the_internal_entry_refuses_a_reader(self):
        full = await P.build()
        async with full.sessionmaker() as session:
            with pytest.raises(TypeError):
                await load_attention(session, tenant_id=full.tenant,
                                     learning=learning_view(empty_scope(full.tenant)))
            with pytest.raises(TypeError):
                await load_human_attention(
                    session, tenant_id=full.tenant,
                    selection=ReadReach(tenant_id=full.tenant, permissions=("fleet.view",)))

    def test_the_predictive_route_reads_outcomes_under_its_reach(self):
        fn = next(f for f in ast.walk(_tree("api/predictive.py"))
                  if isinstance(f, ast.AsyncFunctionDef) and f.name == "device_risk")
        calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
                 and getattr(n.func, "attr", "") == "list_device_outcome_dicts"]
        assert len(calls) == 1
        scope = {k.arg: k.value for k in calls[0].keywords}.get("scope")
        assert isinstance(scope, ast.Name) and scope.id == "reach"

    def test_every_machine_learning_payload_passes_the_bounded_view(self):
        assert _functions_calling("machine_learning_view") == {
            ("governance.py", "_compose_attention"), ("api/incidents.py", "get_incident")}

    def test_one_cohort_function_and_no_second_coarsening(self):
        """D-P2 / D-P9: Model B with the SAME function; no Model D bands."""
        assert _functions_calling("cohort_failure_rates") == {
            ("governance.py", "_compose_attention"), ("api/predictive.py", "device_risk")}
        for rel in ("api/predictive.py", "predictive.py", "attention.py"):
            text = (SRC / rel).read_text()
            assert "band_rate" not in text and "band_confidence" not in text, rel


# ---------------------------------------------------------------------------
# D-P10: the reader's own window is F-E2-4's
# ---------------------------------------------------------------------------


async def _bulk(stack, site: str, device: str, n: int, *, outcome: str, days_ago: int) -> None:
    base = P.T0 - timedelta(days=days_ago)
    async with stack.sessionmaker() as session:
        for start in range(0, n, 10_000):
            await session.execute(insert(CCOutcomeHistory), [
                {"site_id": stack.site(site), "action_id": stack.tagged(f"win-{site}-{k}"),
                 "action_type": "SEL_CLEAR", "device_agent_id": P.agent(stack, device),
                 "vendor": "Dell", "model": E.R750, "outcome": outcome,
                 "fault_resolved": outcome == "SUCCESS", "actor": "a3040",
                 "recorded_at": base + timedelta(seconds=k), "ingested_at": base}
                for k in range(start, min(n, start + 10_000))
            ])
        await session.commit()


class TestTheWindowIsF_E2_4s:
    @pytest.mark.xfail(strict=True, reason=(
        "F-E2-4 (A30.40 D-P10): the predictive read keeps the OLDEST 50000 rows "
        "of the reader's own set. Canonical reach is applied before the window, "
        "so hidden volume never decides membership; the window itself is the "
        "F-E2-4 correctness slice's to remove, with the 10k window."))
    async def test_a_reader_with_more_than_50000_outcomes_counts_them_all(self):
        full = await P.build()
        await _bulk(full, "A", "a1", 50_000, outcome="SUCCESS", days_ago=1)
        body = await P.predictive(full, "h-site-a")
        assert body["outcomes_considered"] == _oracle_count("h-site-a") + 50_000

    async def test_hidden_volume_does_not_decide_the_readers_window(self):
        """What IS claimed at any size: 50000 hidden rows OLDER than every
        site-A row do not push one site-A row out of a site-A reader's set."""
        full = await P.build()
        before = await _both(full, "h-site-a")
        await _bulk(full, "C", "c1", 50_000, outcome="FAILURE", days_ago=400)
        assert await _both(full, "h-site-a") == before
        tenant = await P.predictive(full, "h-owner")
        assert tenant["outcomes_considered"] == 50_000   # the window exists
