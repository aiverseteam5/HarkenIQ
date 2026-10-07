"""S3-E2 (spec A30.39): historical proposal evidence projection.

A stored proposal's `evidence` and `rationale` ARE `decision_evidence_at_creation`,
and every writer since A1 composed them over the WHOLE TENANT: an outcome
statistic, an attention rank and score, and a closing sentence restating the
statistic ("... across 40 executions in this tenant"). Before this slice every
human projection returned them as stored, so a site-A approver read the
tenant's totals -- and a site-A machine's dry-run read them too.

    decision_evidence_at_creation  -- immutable; composed over the tenant;
                                      a scoped reader reads it through an
                                      allow-list, never rewritten.
    viewer_projected_evidence      -- the reader's CURRENT track record,
                                      over exactly the outcomes their
                                      current canonical reach reads.

How it is proven here:

* the READER MATRIX -- tenant, org, site, device and device_class humans, an
  approver with no fleet.view, and machines -- over every human surface and
  the machine dry-run, each narrowing with its control;
* DELETION EQUIVALENCE for the viewer block, including device and
  device_class personas (D6 as amended): a scoped reader of the full estate
  reads what the tenant owner reads of the estate with everything outside
  that reader's reach deleted, checked against an independent oracle;
* IMMUTABILITY: every stored proposal serialised before and after every
  read, every grant change and every topology change;
* SENTINELS no scoped reader and no machine may ever be handed, with the
  tenant owner as control;
* the GOLDEN recorded from `main`: a tenant-wide reader's `evidence` and
  `rationale` are byte-identical;
* STRUCTURAL pins: the writer's grammar, the one loader, the dispatch gate
  reads no evidence.
"""

from __future__ import annotations

import ast
import inspect
import json
import pathlib
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import insert, select

import harkeniq_cc
from harkeniq_cc import governance
from harkeniq_cc.db.models import (
    CCFleetCache,
    CCOutcomeHistory,
    CCSite,
)
from harkeniq_cc.governance import (
    AutonomyView,
    load_proposal_evidence_view,
    require_proposal_evidence_view,
)
from harkeniq_cc.proposal_evidence import (
    CREATION_BASIS_TENANT,
    CREATION_CONDITION_KEYS,
    MACHINE_RATIONALE_NOTE,
    SCOPE_BROADER,
    SCOPE_FULLY_VISIBLE,
    UNAVAILABLE_NO_REACH,
    WITHHELD_TRACK_RECORD,
    machine_dry_run_evidence,
    machine_rationale,
    rationale_head,
    scoped_creation_evidence,
    scoped_rationale,
    track_record_clause,
    withheld_rationale,
)

from tests.unit.cc import s3e2_estate as E
from tests.unit.cc.s3_estate import OWNER

PACKAGE = pathlib.Path(harkeniq_cc.__file__).parent
GOLDEN = pathlib.Path(__file__).parent / "golden" / "a30_39_tenant_wide_proposals.json"

class _FakeSM:
    """Accepts a dispatch; never touches a network."""

    def __init__(self, *_a, **_kw):
        pass

    async def dispatch_action(self, endpoint, token, **kw):
        return {"accepted": True, "directive_id": "dir-e2", "reason": ""}


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    from harkeniq_cc import agent_runtime
    from harkeniq_cc.api import approvals as approvals_api

    monkeypatch.setattr(agent_runtime, "SMClient", _FakeSM)
    monkeypatch.setattr(approvals_api, "SMClient", _FakeSM)


#: The withheld set a scoped human reads, whatever was stored.
SCOPED_WITHHELD = {"attention", "outcome_evidence"}


def _viewer(item: dict) -> dict:
    """A viewer block without its clock."""
    out = dict(item["viewer_projected_evidence"])
    out.pop("as_of", None)
    return out


async def _queue(stack, subject=OWNER, role="tenant_owner") -> list[dict]:
    res = await stack.as_person(subject, role).get("/api/approvals/")
    assert res.status_code == 200, res.text
    return [a["proposal"] for a in res.json()["actions"] if a.get("origin") == "agent"]


def _assert_scoped_creation(item: dict, stored) -> None:
    """What a reader whose reach is not the tenant must read (D3/D4/D5)."""
    evidence = item["evidence"]
    assert evidence["outcome_evidence"] is None
    assert evidence["attention"] is None
    assert evidence["projection"] == "scoped"
    assert SCOPED_WITHHELD <= set(evidence["withheld"])
    assert evidence["withheld"] == sorted(evidence["withheld"])
    for key in evidence:
        assert key in CREATION_CONDITION_KEYS or key in (
            "attention", "outcome_evidence", "learned_signals", "projection",
            "withheld",
        ), key
    assert item["evidence_scope"] == SCOPE_BROADER
    assert item["creation_basis"] == CREATION_BASIS_TENANT
    # The rationale never restates the stored statistic.
    assert "executions in this tenant" not in item["rationale"]
    assert item["rationale"] == scoped_rationale(
        stored.rationale, stored.evidence,
        action_type=stored.action_type, device_agent_id=stored.device_agent_id,
    )


# ---------------------------------------------------------------------------
# 1. The writer's grammar (D2)
# ---------------------------------------------------------------------------


def _old_rationale(agent_name, device, condition, candidate, class_row) -> str:
    """The pre-split `_rationale`, restated verbatim from `main` -- the oracle
    the split must reproduce byte for byte."""
    ev = class_row.get("evidence") or {}
    rate = ev.get("success_rate")
    device_label = getattr(device, "agent_name", "") or device.agent_id
    detail = condition["detail"]
    where = "" if device_label and device_label in detail else f" on {device_label}"
    head = (
        f"{agent_name} observed {detail}{where} and recommends "
        f"{candidate['action_type'].replace('_', ' ').lower()}: "
        f"{candidate['because']}."
    )
    if rate is not None:
        head += (
            f" This class has succeeded {rate:.0%} of the time across "
            f"{ev.get('executions', 0)} executions in this tenant."
        )
    elif ev.get("executions"):
        head += (
            f" The tenant has only {ev['executions']} recorded execution(s) "
            f"of this class, too few to judge a success rate."
        )
    else:
        head += " This tenant has no recorded outcome for this class yet."
    return head


_EVIDENCES = [
    None, {}, {"executions": 0},
    {"executions": 3, "success_rate": None},
    {"executions": 40, "success_rate": 0.575},
    {"executions": 918273645, "success_rate": 0.8761},
    {"executions": 7, "success_rate": 1.0},
]


class TestTheWritersGrammar:
    @pytest.mark.parametrize("evidence", _EVIDENCES, ids=str)
    @pytest.mark.parametrize("label,detail", [
        ("node-7", "fan CRITICAL on node-7"), ("node-7", "log saturated"),
        ("", "log saturated"),
    ])
    def test_the_split_sentence_is_byte_identical(self, evidence, label, detail):
        from types import SimpleNamespace as NS

        from harkeniq_cc.operational_agent import _rationale

        device = NS(agent_name=label, agent_id="dev-1")
        condition = {"detail": detail}
        candidate = {"action_type": "SEL_CLEAR", "because": "the log is full"}
        row = {"evidence": evidence}
        assert _rationale("Agent", device, condition, candidate, row) == \
            _old_rationale("Agent", device, condition, candidate, row)
        assert _rationale("Agent", device, condition, candidate, row) == (
            rationale_head("Agent", device, condition, candidate)
            + track_record_clause(evidence)
        )

    def test_the_head_takes_no_evidence(self):
        """By construction: nothing that carries a count reaches the head,
        so stripping the clause is a COMPLETE reduction (A30.28's rule)."""
        params = list(inspect.signature(rationale_head).parameters)
        assert params == ["agent_name", "device", "condition", "candidate"]
        source = inspect.getsource(rationale_head)
        body = source.split('"""')[2]
        for word in ("evidence", "executions", "success_rate", "class_row"):
            assert word not in body, word

    def test_the_clause_is_the_only_evidence_derived_text(self):
        assert list(inspect.signature(track_record_clause).parameters) == [
            "outcome_evidence"
        ]

    def test_govern_proposal_still_writes_through_the_split(self):
        from harkeniq_cc import operational_agent

        source = inspect.getsource(operational_agent._rationale)
        assert "rationale_head(" in source and "track_record_clause(" in source

    def test_the_stored_basis_is_the_whole_tenant(self):
        """D2, pinned: the evidence a proposal freezes comes from the
        whole-tenant composition, so a change of basis fails here and must
        arrive by amendment with a marker."""
        from harkeniq_cc import operational_agent

        source = inspect.getsource(operational_agent.govern_proposal)
        assert "assessments.evidence_rows()" in source
        assert '"outcome_evidence": evidence_row.get("evidence")' in source
        evidence_rows = inspect.getsource(governance.SiteAssessments.evidence_rows)
        assert "self._compose(None," in evidence_rows


# ---------------------------------------------------------------------------
# 2. The pure projections (D3, D5, D9)
# ---------------------------------------------------------------------------


class TestTheCreationRecordForAScopedReader:
    def test_the_condition_facts_pass_and_nothing_composed_over_the_estate(self):
        stored = E.sentinel_evidence(legacy_tenant_stat=E.HIDDEN_KEY)
        out = scoped_creation_evidence(stored, [{"statement": "kept"}])
        assert out["outcome_evidence"] is None and out["attention"] is None
        assert out["learned_signals"] == [{"statement": "kept"}]
        assert out["withheld"] == ["attention", "legacy_tenant_stat", "outcome_evidence"]
        assert out["projection"] == "scoped"
        for key in CREATION_CONDITION_KEYS:
            assert out[key] == stored[key], key
        assert E.sentinel_hits(out) == []

    @pytest.mark.parametrize("stored", [None, {}, [], "garbage", 7])
    def test_the_shape_never_varies(self, stored):
        out = scoped_creation_evidence(stored, None)
        assert out["outcome_evidence"] is None and out["attention"] is None
        assert out["withheld"] == ["attention", "outcome_evidence"]
        assert "learned_signals" not in out

    def test_a_writer_key_added_later_is_withheld_until_named(self):
        out = scoped_creation_evidence({"observed": "x", "future_stat": 99}, [])
        assert "future_stat" not in out and "future_stat" in out["withheld"]


class TestTheRationaleForAScopedReader:
    def test_the_writers_clause_is_replaced_by_one_constant(self):
        stored = E.HEAD + E.HIDDEN_CLAUSE
        out = scoped_rationale(stored, E.sentinel_evidence(),
                               action_type="SEL_CLEAR", device_agent_id="node-s3-a")
        assert out == E.HEAD + WITHHELD_TRACK_RECORD
        assert E.sentinel_hits(out) == [] and E.HIDDEN_PERCENT not in out

    @pytest.mark.parametrize("outcome", [
        None, {}, {"executions": 3, "success_rate": None}, {"executions": 0},
    ])
    def test_every_clause_the_writer_can_produce_is_removed(self, outcome):
        """Including "no recorded outcome in this tenant": a tenant-wide
        ZERO is a tenant-wide fact too."""
        stored = E.HEAD + track_record_clause(outcome)
        evidence = {"outcome_evidence": outcome}
        out = scoped_rationale(stored, evidence, action_type="SEL_CLEAR",
                               device_agent_id="d")
        assert out == E.HEAD + WITHHELD_TRACK_RECORD
        assert "tenant" not in out.replace("the whole tenant", "")

    @pytest.mark.parametrize("stored,evidence", [
        (E.SENTINEL_ROWS["malformed"][0], E.sentinel_evidence()),     # no clause
        (E.HEAD + E.HIDDEN_CLAUSE, None),                              # no evidence
        (E.HEAD + E.HIDDEN_CLAUSE, "not a mapping"),
        (E.HEAD + E.HIDDEN_CLAUSE, {"outcome_evidence": "garbage"}),
        (E.HEAD + E.HIDDEN_CLAUSE, {"outcome_evidence": {"success_rate": "x"}}),
        # Stored totals that differ from the clause the sentence carries.
        (E.HEAD + E.HIDDEN_CLAUSE, {"outcome_evidence": {"executions": 1,
                                                         "success_rate": 1.0}}),
        (E.HIDDEN_CLAUSE, E.sentinel_evidence()),                      # empty head
        ("", E.sentinel_evidence()),
        (None, E.sentinel_evidence()),
    ])
    def test_anything_else_fails_closed_to_a_sentence_about_the_target_only(
            self, stored, evidence):
        out = scoped_rationale(stored, evidence, action_type="SEL_CLEAR",
                               device_agent_id="node-s3-a")
        assert out == withheld_rationale("SEL_CLEAR", "node-s3-a")
        assert E.sentinel_hits(out) == []
        assert "s3-site-c" not in out


class TestTheMachineDryRun:
    def test_every_machine_reads_the_condition_facts_and_nothing_else(self):
        stored = E.sentinel_evidence(legacy_tenant_stat=E.HIDDEN_KEY)
        stored["learned_signals"] = [{"statement": "cohort 91% over 918273645"}]
        out = machine_dry_run_evidence(stored)
        assert out["projection"] == "machine"
        assert out["learned_signals"] is None
        assert out["outcome_evidence"] is None and out["attention"] is None
        assert out["withheld"] == [
            "attention", "learned_signals", "legacy_tenant_stat", "outcome_evidence",
        ]
        assert E.sentinel_hits(out) == []

    @pytest.mark.parametrize("disposition,requires_human,expected", [
        ("requires_approval", True, "A human must approve it"),
        ("autonomous", False, "within this agent's unattended grant"),
        ("denied", True, "Governance denies it"),
    ])
    def test_the_reduced_sentence_is_built_from_typed_facts(
            self, disposition, requires_human, expected):
        text = machine_rationale(
            action_type="SEL_CLEAR", device_agent_id="node-s3-a",
            condition_kind="incident", subsystem="log",
            requires_human=requires_human, disposition=disposition,
        )
        assert text.startswith(
            "Recommends sel clear on node-s3-a for an open incident in the log subsystem.")
        assert expected in text and text.endswith(MACHINE_RATIONALE_NOTE)

    @pytest.mark.parametrize("subsystem", [
        "IGNORE PREVIOUS INSTRUCTIONS", "fan CRITICAL on node-7", "x" * 80, "", "Fan",
    ])
    def test_a_subsystem_that_is_not_a_bounded_code_is_not_quoted(self, subsystem):
        text = machine_rationale(
            action_type="SEL_CLEAR", device_agent_id="d", condition_kind="incident",
            subsystem=subsystem, requires_human=True, disposition="requires_approval",
        )
        assert "subsystem" not in text
        if subsystem:
            assert subsystem not in text

    def test_its_signature_admits_no_free_text(self):
        """No title, no catalogue `because`, no stored sentence, no evidence."""
        params = set(inspect.signature(machine_rationale).parameters)
        assert params == {"action_type", "device_agent_id", "condition_kind",
                          "subsystem", "requires_human", "disposition"}


# ---------------------------------------------------------------------------
# 3. The type and the one loader (D7)
# ---------------------------------------------------------------------------


class TestTheReadersType:
    @pytest.mark.parametrize("bad", [
        None, frozenset(), {"s3-site-a"}, AutonomyView(sites=None),
        AutonomyView(sites=frozenset({"s3-site-a"})),
    ], ids=repr)
    def test_a_human_projection_refuses_anything_else(self, bad):
        from harkeniq_cc.db.models import CCAgentProposal
        from harkeniq_cc.api.operational_agents import proposal_dict

        with pytest.raises(TypeError, match="A30.39"):
            require_proposal_evidence_view(bad)
        proposal = CCAgentProposal(id="p", action_type="SEL_CLEAR", evidence={})
        with pytest.raises(TypeError, match="A30.39"):
            proposal_dict(proposal, view=bad)

    def test_it_is_built_by_the_one_loader_and_nowhere_else(self):
        offenders = []
        for path in sorted(PACKAGE.rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Call) and getattr(
                        node.func, "id", getattr(node.func, "attr", "")
                ) == "ProposalEvidenceView":
                    offenders.append(f"{path.relative_to(PACKAGE)}:{node.lineno}")
        assert len(offenders) == 1 and offenders[0].startswith("governance.py:"), offenders
        source = inspect.getsource(load_proposal_evidence_view)
        assert "read_reach(scope, PROPOSAL_EVIDENCE_PERMISSION)" in source
        # A30.41: the canonical read is now an exact tally -- still under
        # the reader's reach, now over every row.
        assert "tally(" in source and "scope=reach" in source
        assert governance.PROPOSAL_EVIDENCE_PERMISSION == "fleet.view"

    def test_the_loader_resolves_nothing(self):
        """No second resolver: the reach is S2's, the rows are the canonical
        outcome read's, and no site is synthesized for a device."""
        source = inspect.getsource(load_proposal_evidence_view).split('"""')[2]
        for forbidden in ("resolve(", "load_scope(", "covers_site", "site_ids",
                          "contextual", "FleetCacheRepo", "SiteRepo"):
            assert forbidden not in source, forbidden

    def test_every_human_surface_loads_it(self):
        from harkeniq_cc.api import approvals, operational_agents

        for fn in (operational_agents.get_agent, operational_agents.list_proposals,
                   operational_agents.dry_run_agent, approvals.list_pending):
            assert "load_proposal_evidence_view(" in inspect.getsource(fn), fn
        assert "load_proposal_evidence_view(" in inspect.getsource(approvals._evidence_view)
        assert "_evidence_view(" in inspect.getsource(approvals._decide_agent_proposal)


# ---------------------------------------------------------------------------
# 4. The golden: a tenant-wide reader reads the stored bytes (D8)
# ---------------------------------------------------------------------------


class TestTheTenantWideReaderIsUnchanged:
    async def test_byte_identical_to_the_golden_recorded_from_main(self):
        stack, ids = await E.scenario()
        golden = json.loads(GOLDEN.read_text())["tenant_wide"]
        assert await E.tenant_wide_payloads(stack, ids) == golden

    async def test_and_equal_to_the_stored_row_within_one_request(self):
        stack, ids = await E.scenario()
        for item in await _queue(stack):
            stored = await E.stored(stack, item["proposal_id"])
            assert item["evidence"] == (stored.evidence or {})
            assert item["rationale"] == stored.rationale
            assert item["evidence_scope"] == SCOPE_FULLY_VISIBLE
            assert item["creation_basis"] == CREATION_BASIS_TENANT
            assert _viewer(item) == {
                "basis": "current_reach", "unavailable_reason": None,
                "outcome_evidence": E.expected_track_record("tenant"),
            }

    async def test_the_golden_carries_the_facts_a_scoped_reader_is_denied(self):
        """Non-vacuity: the stored tenant totals and the sentinels ARE in what
        the tenant reads, so their absence elsewhere means something."""
        golden = json.loads(GOLDEN.read_text())["tenant_wide"]
        assert golden["queue"]["evaluated"]["evidence"]["outcome_evidence"]["executions"] == 40
        assert "40 executions in this tenant" in golden["queue"]["evaluated"]["rationale"]
        assert E.sentinel_hits(golden["queue"]["clean"]) != []
        assert E.HIDDEN_RATIONALE in golden["queue"]["malformed"]["rationale"]


# ---------------------------------------------------------------------------
# 5. The reader matrix, over every human surface
# ---------------------------------------------------------------------------


class TestEveryHumanReaderOnTheQueue:
    @pytest.mark.parametrize("name", sorted(E.PERSONAS))
    async def test_the_queue(self, name):
        stack = await E.build()
        agent_id = await E.agent(stack, name="S3E2 Queue", scopes=[("site", "A")])
        by_device = await E.seed_device_proposals(stack, agent_id)
        subject = await E.persona(stack, name)
        items = await _queue(stack, subject, "site_admin")
        seen = {i["device_agent_id"] for i in items}
        expected = {
            stack.tagged(dev) for dev, (site_key, _c) in E.FLEET.items()
            if E.approve_readable(name, site_key, dev)
        }
        assert seen == expected                      # B0b's owner rule, unchanged
        want = E.expected_track_record(name)
        for item in items:
            stored = await E.stored(stack, item["proposal_id"])
            if E.tenant_wide(name):
                assert item["evidence"] == stored.evidence
                assert item["rationale"] == stored.rationale
                assert item["evidence_scope"] == SCOPE_FULLY_VISIBLE
            else:
                _assert_scoped_creation(item, stored)
                assert item["rationale"] == E.HEAD + WITHHELD_TRACK_RECORD
                assert E.sentinel_hits(item) == [], E.sentinel_hits(item)
            viewer = _viewer(item)
            if want is None:
                assert viewer == {"basis": "current_reach", "outcome_evidence": None,
                                  "unavailable_reason": UNAVAILABLE_NO_REACH}
            else:
                assert viewer == {"basis": "current_reach", "outcome_evidence": want,
                                  "unavailable_reason": None}
        # The stored bytes are what they were before anybody read them.
        for dev, pid in by_device.items():
            stored = await E.stored(stack, pid)
            assert stored.evidence["outcome_evidence"] == E.HIDDEN_OUTCOME_EVIDENCE

    def test_the_matrix_is_not_vacuous(self):
        """Every native reach type is present, and the expected track records
        are pairwise distinct where the reach differs."""
        kinds = {t for grants in E.PERSONAS.values() for t, _r, _s in grants}
        assert {"tenant", "org_unit", "site", "device", "device_class"} <= kinds
        got = {n: (E.expected_track_record(n) or {}).get("executions")
               for n in ("tenant", "org_ab", "site_a", "site_b", "device_a",
                         "class_server", "class_switch", "device_a2")}
        assert got == {"tenant": 40, "org_ab": 27, "site_a": 11, "site_b": 16,
                       "device_a": 7, "class_server": 23, "class_switch": 12,
                       "device_a2": 3}


class TestEveryHumanSurfaceCarriesTheContract:
    async def _surfaces(self, stack, ids, subject, role="site_admin") -> dict:
        me = stack.as_person(subject, role)
        agent_id = ids["agent"]
        return {
            "queue": [a["proposal"] for a in
                      (await me.get("/api/approvals/")).json()["actions"]
                      if a.get("origin") == "agent"],
            "detail": (await me.get(f"/api/operational-agents/{agent_id}")).json()
            ["proposals"],
            "list": (await me.get(f"/api/operational-agents/{agent_id}/proposals"))
            .json()["proposals"],
        }

    @pytest.mark.parametrize("name", ["site_a", "org_ab", "a_plus_approve_c",
                                      "a_plus_device_c"])
    async def test_a_scoped_reader_on_every_surface(self, name):
        stack, ids = await E.scenario()
        subject = await E.persona(stack, name)
        surfaces = await self._surfaces(stack, ids, subject)
        for where, items in surfaces.items():
            by_id = {i["proposal_id"]: i for i in items}
            for label in ("evaluated", "clean", "malformed", "extra_key", "no_evidence"):
                item = by_id[ids[label]]
                stored = await E.stored(stack, ids[label])
                _assert_scoped_creation(item, stored)
                assert E.sentinel_hits(item) == [], (where, label)
                assert _viewer(item)["outcome_evidence"] == E.expected_track_record(name)
            assert by_id[ids["malformed"]]["rationale"] == withheld_rationale(
                "SEL_CLEAR", stack.tagged("node-s3-a"))
            assert by_id[ids["no_evidence"]]["rationale"] == withheld_rationale(
                "SEL_CLEAR", stack.tagged("node-s3-a"))
            assert "legacy_tenant_stat" in by_id[ids["extra_key"]]["evidence"]["withheld"]
            assert by_id[ids["evaluated"]]["rationale"].endswith(WITHHELD_TRACK_RECORD)

    async def test_the_tenant_owner_is_the_control_on_every_surface(self):
        stack, ids = await E.scenario()
        surfaces = await self._surfaces(stack, ids, OWNER, "tenant_owner")
        for where, items in surfaces.items():
            by_id = {i["proposal_id"]: i for i in items}
            assert E.sentinel_hits(by_id[ids["clean"]]) != [], where
            assert by_id[ids["evaluated"]]["evidence"]["outcome_evidence"]["executions"] == 40

    async def test_the_human_dry_run(self):
        stack, ids = await E.scenario()
        path = f"/api/operational-agents/{ids['agent']}/dry-run"
        site_a = await E.persona(stack, "site_a")
        mine = (await stack.as_person(site_a, "site_admin").get(path)).json()
        owner = (await stack.as_person().get(path)).json()
        (preview,) = mine["would_propose"]
        (control,) = owner["would_propose"]
        assert preview["evidence"]["outcome_evidence"] is None
        assert preview["evidence"]["attention"] is None
        assert preview["evidence_scope"] == SCOPE_BROADER
        assert preview["rationale"].endswith(WITHHELD_TRACK_RECORD)
        assert "40" not in preview["rationale"]
        assert _viewer(preview)["outcome_evidence"] == E.expected_track_record("site_a")
        # CONTROL
        assert control["evidence"]["outcome_evidence"]["executions"] == 40
        assert "40 executions in this tenant" in control["rationale"]
        assert control["evidence_scope"] == SCOPE_FULLY_VISIBLE
        assert _viewer(control)["outcome_evidence"] == E.expected_track_record("tenant")
        # The head -- what the agent saw and recommends -- is the same text.
        assert preview["rationale"][: -len(WITHHELD_TRACK_RECORD)] == \
            control["rationale"][: control["rationale"].index(" This class")]

    async def test_a_generated_diagnosis_never_reaches_any_proposal(self):
        """Case 12: the only trace of the diagnosis is a boolean."""
        stack, ids = await E.scenario()
        for subject, role in ((OWNER, "tenant_owner"),
                              (await E.persona(stack, "site_a"), "site_admin")):
            surfaces = await self._surfaces(stack, ids, subject, role)
            assert E.GENERATED not in json.dumps(surfaces)
            evaluated = next(i for i in surfaces["queue"]
                             if i["proposal_id"] == ids["evaluated"])
            assert evaluated["evidence"]["has_diagnosis"] is True


# ---------------------------------------------------------------------------
# 6. Case 16: an approver who holds no fleet.view
# ---------------------------------------------------------------------------


class TestAnApproverWithoutFleetView:
    async def test_reads_the_decision_and_no_track_record_either_way(self):
        stack, ids = await E.scenario()
        subject = await E.persona(stack, "approve_only_a")
        items = {i["proposal_id"]: i for i in await _queue(stack, subject, "site_admin")}
        item = items[ids["evaluated"]]
        stored = await E.stored(stack, ids["evaluated"])
        _assert_scoped_creation(item, stored)
        assert item["viewer_projected_evidence"]["outcome_evidence"] is None
        assert item["viewer_projected_evidence"]["unavailable_reason"] == \
            UNAVAILABLE_NO_REACH
        # A30.26/A30.28 unchanged: no site's learned signal, the cohort's
        # conclusion bounded.
        statements = [s["statement"] for s in item["evidence"]["learned_signals"]]
        assert statements == ["cohort-knowledge"]
        assert E.sentinel_hits(item) == []

    async def test_no_fleet_view_reads_no_outcome_row_at_all(self):
        """Reader SHAPE, not content: the loader does not read the table."""
        stack = await E.build()
        subject = await E.persona(stack, "approve_only_a")
        view = await E.evidence_view(stack, subject)
        assert view.reach_empty and view.outcomes == ()
        assert not view.tenant_wide


# ---------------------------------------------------------------------------
# 7. Deletion equivalence for the viewer block, every native reach type (D6)
# ---------------------------------------------------------------------------

REACHERS = sorted(n for n in E.PERSONAS if E.has_fleet_view_reach(n))


class TestTheViewerBlockIsDeletionEquivalent:
    @pytest.mark.parametrize("name", REACHERS)
    async def test_a_scoped_reader_reads_the_estate_without_what_it_cannot_read(
            self, name):
        full = await E.build()
        subject = await E.persona(full, name)
        mine = await E.evidence_view(full, subject)

        deleted = await E.build(keep=E.keep_for(name))
        oracle = await E.evidence_view(deleted, OWNER, "tenant_owner")
        assert oracle.tenant_wide
        for action in ("SEL_CLEAR", "BMC_RESET", "POWER_CYCLE"):
            got, want = mine.viewer(action), oracle.viewer(action)
            got.pop("as_of"), want.pop("as_of")
            assert got == want, (name, action)
            assert got["outcome_evidence"] == E.expected_track_record(name, action)

    async def test_the_control_moves(self):
        """The tenant owner of the FULL estate reads what no scoped reader
        does -- so equality above is not the empty set agreeing with itself."""
        full = await E.build()
        owner = await E.evidence_view(full, OWNER, "tenant_owner")
        assert owner.viewer("SEL_CLEAR")["outcome_evidence"]["executions"] == 40
        assert owner.viewer("BMC_RESET")["outcome_evidence"]["executions"] == 6

    @pytest.mark.parametrize("name", ["device_a", "class_server", "class_switch"])
    async def test_device_and_class_readers_never_read_a_site_total(self, name):
        """D6 as amended: exactly the authorized object set -- never a sibling
        device, never the containing site, never the tenant."""
        full = await E.build()
        subject = await E.persona(full, name)
        got = (await E.evidence_view(full, subject)).viewer("SEL_CLEAR")
        executions = got["outcome_evidence"]["executions"]
        site_totals = {E.expected_track_record(s)["executions"]
                       for s in ("site_a", "site_b", "org_ab", "tenant")}
        assert executions not in site_totals
        assert got["unavailable_reason"] is None


# ---------------------------------------------------------------------------
# 8. Immutability and current reach: cases 6, 7, 8, 9, 13, 14, 15
# ---------------------------------------------------------------------------


class TestHistoryDoesNotMoveAndTheViewDoes:
    async def _scene(self):
        stack, ids = await E.scenario()
        before = await E.stored_bytes(stack)
        return stack, ids, before

    async def _item(self, stack, pid, subject, role="site_admin"):
        items = {i["proposal_id"]: i for i in await _queue(stack, subject, role)}
        return items.get(pid)

    async def test_reading_writes_nothing(self):
        stack, ids, before = await self._scene()
        for name in E.PERSONAS:
            subject = await E.persona(stack, name)
            await _queue(stack, subject, "site_admin")
            me = stack.as_person(subject, "site_admin")
            await me.get(f"/api/operational-agents/{ids['agent']}/proposals")
            await me.get(f"/api/operational-agents/{ids['agent']}")
            await me.get(f"/api/operational-agents/{ids['agent']}/dry-run")
        assert await E.stored_bytes(stack) == before

    async def test_a_grant_narrowed_after_creation(self):
        """Case 6: org AB, then site A only. The creation record is the same
        withheld shape; the current track record follows the grant."""
        stack, ids, before = await self._scene()
        subject = await E.persona(stack, "org_ab")
        first = await self._item(stack, ids["evaluated"], subject)
        assert _viewer(first)["outcome_evidence"]["executions"] == 27
        from harkeniq_cc.db.models import CCScopeGrant

        async with stack.sessionmaker() as session:
            (grant,) = (await session.execute(select(CCScopeGrant).where(
                CCScopeGrant.principal_ref == subject))).scalars().all()
            grant_id = grant.id
        await stack.lapse(grant_id, how="revoked")
        await stack.grant(subject, "site", stack.site("A"))
        second = await self._item(stack, ids["evaluated"], subject)
        assert _viewer(second)["outcome_evidence"]["executions"] == 11

        def without_signals(evidence):
            return {k: v for k, v in evidence.items() if k != "learned_signals"}

        # The withheld creation record reads the same; the learned signals
        # follow current reach (S3/S4, unchanged): site B's leaves with B.
        assert without_signals(first["evidence"]) == without_signals(second["evidence"])
        assert first["rationale"] == second["rationale"]
        assert "signal-B" in json.dumps(first["evidence"]["learned_signals"])
        assert "signal-B" not in json.dumps(second["evidence"]["learned_signals"])
        assert await E.stored_bytes(stack) == before

    @pytest.mark.parametrize("how", ["revoked", "expired"])
    async def test_a_grant_revoked_or_expired_after_creation(self, how):
        """Case 7: the proposal leaves the reader's queue (B0b) and the
        stored row does not move."""
        stack, ids, before = await self._scene()
        subject = await E.persona(stack, "site_a")
        assert await self._item(stack, ids["evaluated"], subject) is not None
        from harkeniq_cc.db.models import CCScopeGrant

        async with stack.sessionmaker() as session:
            (grant,) = (await session.execute(select(CCScopeGrant).where(
                CCScopeGrant.principal_ref == subject))).scalars().all()
            grant_id = grant.id
        await stack.lapse(grant_id, how=how)
        assert await _queue(stack, subject, "site_admin") == []
        assert await E.stored_bytes(stack) == before

    async def test_a_site_reassigned_and_a_site_deleted_after_creation(self):
        """Case 8 and 15: B moves to region CD, then is deleted. The org-AB
        reader's current record follows; the tenant owner's creation record
        still says 40 -- historical evidence larger than current evidence
        (case 13), from a site that no longer exists."""
        stack, ids, before = await self._scene()
        subject = await E.persona(stack, "org_ab")
        async with stack.sessionmaker() as session:
            site_b = await session.get(CCSite, stack.site("B"))
            site_b.org_unit_id = stack.regions["cd"]
            await session.commit()
        after_move = await self._item(stack, ids["evaluated"], subject)
        assert _viewer(after_move)["outcome_evidence"]["executions"] == 11
        async with stack.sessionmaker() as session:
            await session.delete(await session.get(CCSite, stack.site("B")))
            await session.commit()
        owner = await self._item(stack, ids["evaluated"], OWNER, "tenant_owner")
        assert owner["evidence"]["outcome_evidence"]["executions"] == 40
        assert _viewer(owner)["outcome_evidence"]["executions"] == 40 - 16
        assert await E.stored_bytes(stack) == before

    async def test_a_device_moved_after_creation(self):
        """Case 9: node-s3-a moves from A to B. The device reader's proposal
        at A is no longer theirs (B0b); their current record is now the
        rows node-s3-a owns at B; the stored device snapshot is unchanged."""
        stack, ids, before = await self._scene()
        subject = await E.persona(stack, "device_a")
        assert await self._item(stack, ids["evaluated"], subject) is not None
        assert (await E.evidence_view(stack, subject)).viewer(
            "SEL_CLEAR")["outcome_evidence"]["executions"] == 7
        async with stack.sessionmaker() as session:
            row = (await session.execute(select(CCFleetCache).where(
                CCFleetCache.agent_id == stack.tagged("node-s3-a")))).scalar_one()
            row.site_id = stack.site("B")
            await session.commit()
        assert await self._item(stack, ids["evaluated"], subject) is None
        assert (await E.evidence_view(stack, subject)).viewer(
            "SEL_CLEAR")["outcome_evidence"]["executions"] == 3
        assert await E.stored_bytes(stack) == before

    async def test_the_estate_grows_after_creation(self):
        """Case 14: current evidence LARGER than the creation record. The two
        blocks are independent; no arithmetic joins them."""
        stack, ids, before = await self._scene()
        async with stack.sessionmaker() as session:
            await session.execute(insert(CCOutcomeHistory), [
                {"site_id": stack.site("A"), "action_id": f"grow-{n}",
                 "action_type": "SEL_CLEAR", "device_agent_id": stack.tagged("node-s3-a"),
                 "vendor": "Dell", "model": "R750", "outcome": "SUCCESS",
                 "fault_resolved": True, "actor": "seed",
                 "ingested_at": datetime.now(timezone.utc)}
                for n in range(60)
            ])
            await session.commit()
        owner = await self._item(stack, ids["evaluated"], OWNER, "tenant_owner")
        assert owner["evidence"]["outcome_evidence"]["executions"] == 40
        assert _viewer(owner)["outcome_evidence"]["executions"] == 100
        site_a = await E.persona(stack, "site_a")
        mine = await self._item(stack, ids["evaluated"], site_a)
        assert _viewer(mine)["outcome_evidence"]["executions"] == 71
        assert mine["evidence"]["outcome_evidence"] is None
        assert await E.stored_bytes(stack) == before


# ---------------------------------------------------------------------------
# 9. A scoped creation view does not move when a hidden fact does (D11 b)
# ---------------------------------------------------------------------------


class TestHiddenFactsDoNotMoveTheScopedCreationView:
    async def test_not_the_hidden_outcomes_and_not_the_stored_totals(self):
        stack, ids = await E.scenario()
        subject = await E.persona(stack, "site_a")
        before = {i["proposal_id"]: i for i in await _queue(stack, subject, "site_admin")}
        owner_before = {i["proposal_id"]: i for i in await _queue(stack)}

        # Hidden outcomes at C, and the STORED tenant totals themselves.
        async with stack.sessionmaker() as session:
            await session.execute(insert(CCOutcomeHistory), [
                {"site_id": stack.site("C"), "action_id": f"hidden-{n}",
                 "action_type": "SEL_CLEAR", "device_agent_id": stack.tagged("node-s3-c"),
                 "vendor": "Dell", "model": "R750", "outcome": "FAILURE",
                 "fault_resolved": False, "actor": "seed",
                 "ingested_at": datetime.now(timezone.utc)}
                for n in range(25)
            ])
            from harkeniq_cc.db.models import CCAgentProposal

            row = await session.get(CCAgentProposal, ids["clean"])
            other = {"executions": 123456789, "success": 1, "failure": 123456788,
                     "success_rate": 0.0, "resolution_rate": 0.0,
                     "sites_observed": 987654321, "sufficient": True,
                     "window": "all_time"}
            row.evidence = {**row.evidence, "outcome_evidence": other}
            row.rationale = E.HEAD + track_record_clause(other)
            await session.commit()

        after = {i["proposal_id"]: i for i in await _queue(stack, subject, "site_admin")}
        owner_after = {i["proposal_id"]: i for i in await _queue(stack)}
        for pid in before:
            b, a = dict(before[pid]), dict(after[pid])
            b.pop("viewer_projected_evidence"), a.pop("viewer_projected_evidence")
            assert a == b, pid
            assert _viewer(after[pid]) == _viewer(before[pid])   # C is hidden
        # CONTROL: the tenant owner reads both changes.
        assert owner_after[ids["clean"]]["evidence"] != owner_before[ids["clean"]]["evidence"]
        assert _viewer(owner_after[ids["evaluated"]])["outcome_evidence"]["executions"] \
            == 40 + 25


# ---------------------------------------------------------------------------
# 10. Case 19: the approval decision responses
# ---------------------------------------------------------------------------


class TestTheDecisionResponses:
    async def test_deny_approve_and_batch_carry_the_contract(self, monkeypatch):
        stack, ids = await E.scenario()
        subject = await E.persona(stack, "site_a")
        calls = []
        real = governance.load_proposal_evidence_view

        async def counting(*a, **kw):
            calls.append(1)
            return await real(*a, **kw)

        from harkeniq_cc.api import approvals

        monkeypatch.setattr(approvals, "load_proposal_evidence_view", counting)
        async with stack.as_person(subject, "site_admin").client() as client:
            denied = (await client.post(f"/api/approvals/{ids['clean']}/deny")).json()
            batch = (await client.post("/api/approvals/batch", json={
                "action_ids": [ids["malformed"], ids["extra_key"], ids["no_evidence"]],
                "decision": "denied",
            })).json()
        for payload in [denied["proposal"]] + [
                r["detail"]["proposal"] for r in batch["results"]]:
            stored = await E.stored(stack, payload["proposal_id"])
            _assert_scoped_creation(payload, stored)
            assert E.sentinel_hits(payload) == []
            assert _viewer(payload)["outcome_evidence"] == E.expected_track_record("site_a")
        # One load for the single decision, ONE for the whole batch.
        assert len(calls) == 2

    async def test_an_approval_that_is_refused_at_dispatch_carries_it_too(self):
        """The synchronous path: the site-A agent's proposal is approved; the
        response carries the reader's projection whatever the dispatch did."""
        stack, ids = await E.scenario()
        subject = await E.persona(stack, "site_a")
        async with stack.as_person(subject, "site_admin").client() as client:
            res = await client.post(f"/api/approvals/{ids['evaluated']}/approve")
        assert res.status_code == 200, res.text
        payload = res.json()["proposal"]
        stored = await E.stored(stack, ids["evaluated"])
        _assert_scoped_creation(payload, stored)


# ---------------------------------------------------------------------------
# 11. Machines (case 17, 18; D9)
# ---------------------------------------------------------------------------


class TestMachinesReadBoundedConclusionsOnly:
    async def _dry_run(self, stack, agent_id):
        async with stack.as_machine(agent_id).client() as client:
            res = await client.get(f"/api/operational-agents/{agent_id}/dry-run")
        assert res.status_code == 200, res.text
        return res.json()

    @pytest.mark.parametrize("tenant_scoped", [False, True])
    async def test_every_machine_dry_run(self, tenant_scoped):
        stack, ids = await E.scenario()
        agent_id = ids["agent"]
        if tenant_scoped:
            await stack.grant(agent_id, "tenant", "", principal_type="agent", role="")
        (preview,) = (await self._dry_run(stack, agent_id))["would_propose"]
        evidence = preview["evidence"]
        assert evidence["projection"] == "machine"
        for key in ("outcome_evidence", "attention", "learned_signals"):
            assert evidence[key] is None, key
            assert key in evidence["withheld"]
        for key in ("viewer_projected_evidence", "evidence_scope", "creation_basis"):
            assert key not in preview
        assert preview["rationale"] == machine_rationale(
            action_type="SEL_CLEAR", device_agent_id=stack.tagged("node-e2-a2"),
            condition_kind="incident", subsystem="log",
            requires_human=preview["requires_human"],
            disposition=preview["disposition"],
        )
        # The catalogue's free text reaches a machine only as the REAL
        # parameter it is (A22.2, `params.reason`) -- never in the sentence,
        # and nothing composed over outcomes or learning anywhere.
        text = json.dumps({"rationale": preview["rationale"],
                           "evidence": preview["evidence"]})
        for forbidden in ("executions", "cohort-knowledge", "signal-A",
                          "saturated event log hides", "in this tenant", "S3E2 Runtime"):
            assert forbidden not in text, forbidden
        for forbidden in ("executions", "cohort-knowledge", "signal-A", "in this tenant"):
            assert forbidden not in json.dumps(preview), forbidden
        # B2-1's recorded exemption is unchanged: the condition's own title.
        assert evidence["observed"] == "BMC event log saturated"

    async def test_the_human_control_reads_what_the_machine_does_not(self):
        stack, ids = await E.scenario()
        path = f"/api/operational-agents/{ids['agent']}/dry-run"
        (human,) = (await stack.as_person().get(path)).json()["would_propose"]
        assert human["evidence"]["learned_signals"]
        assert human["evidence"]["outcome_evidence"]["executions"] == 40

    async def test_lifecycle_projections_carry_no_evidence_and_no_rationale(self):
        stack, ids = await E.scenario()
        agent_id = ids["agent"]
        base = f"/api/operational-agents/{agent_id}"
        async with stack.as_machine(agent_id).client() as client:
            payloads = {
                "detail": (await client.get(base)).json(),
                "list": (await client.get(f"{base}/proposals")).json(),
                "receipt": (await client.get(
                    f"{base}/proposals/{ids['evaluated']}")).json(),
            }
        for where, payload in payloads.items():
            keys = {leaf for _p, leaf, is_key in E.leaves(payload) if is_key}
            assert not keys & {"evidence", "rationale", "outcome_evidence",
                               "viewer_projected_evidence"}, where
            assert E.sentinel_hits(payload) == [], where


# ---------------------------------------------------------------------------
# 12. What did not move (D12)
# ---------------------------------------------------------------------------


class TestExecutionReadsNoEvidence:
    def test_the_dispatch_gate_reads_no_evidence_field(self):
        from harkeniq_cc import agent_runtime

        for fn in (agent_runtime.revalidate_dispatch, agent_runtime.dispatch_decided):
            tree = ast.parse(inspect.getsource(fn).lstrip())
            attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
            assert not attrs & {"evidence", "rationale"}, fn.__name__

    def test_the_autonomy_composer_is_not_a_consumer(self):
        from harkeniq_cc import autonomy

        assert "proposal_evidence" not in inspect.getsource(autonomy)


# ---------------------------------------------------------------------------
# 13. The 10000-row window was F-E2-4's; A30.41 removed it
# ---------------------------------------------------------------------------


class TestTheWindowIsNotClaimedAway:
    """D11/D14 pinned the reader's own 10,000-row window with a strict xfail
    for the F-E2-4 slice to invert. A30.41 is that slice: the canonical read
    is an exact tally over every row, so the pin is inverted here -- never
    deleted -- and A30.41's module proves the boundary from both sides."""

    async def test_a_reader_with_more_than_10000_outcomes_counts_them_all(self):
        stack = await E.build()
        subject = await E.persona(stack, "site_a")
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        async with stack.sessionmaker() as session:
            await session.execute(insert(CCOutcomeHistory), [
                {"site_id": stack.site("A"), "action_id": f"win-{n}",
                 "action_type": "SEL_CLEAR", "device_agent_id": stack.tagged("node-s3-a"),
                 "vendor": "Dell", "model": "R750", "outcome": "SUCCESS",
                 "fault_resolved": True, "actor": "seed",
                 "ingested_at": base + timedelta(seconds=n)}
                for n in range(10_000)
            ])
            await session.commit()
        view = await E.evidence_view(stack, subject)
        assert view.viewer("SEL_CLEAR")["outcome_evidence"]["executions"] == 10_000 + 11

    async def test_hidden_volume_does_not_decide_the_readers_window(self):
        """What IS claimed at any size: 10000 hidden rows OLDER than every
        site-A row do not push a single site-A row out of the reader's set
        (they did, through `/api/autonomy`'s post-window filter: F-E2-4)."""
        stack = await E.build()
        subject = await E.persona(stack, "site_a")
        base = datetime(2020, 1, 1, tzinfo=timezone.utc)
        async with stack.sessionmaker() as session:
            await session.execute(insert(CCOutcomeHistory), [
                {"site_id": stack.site("C"), "action_id": f"old-{n}",
                 "action_type": "SEL_CLEAR", "device_agent_id": stack.tagged("node-s3-c"),
                 "vendor": "Dell", "model": "R750", "outcome": "FAILURE",
                 "fault_resolved": False, "actor": "seed",
                 "ingested_at": base + timedelta(seconds=n)}
                for n in range(10_000)
            ])
            await session.commit()
        view = await E.evidence_view(stack, subject)
        assert view.viewer("SEL_CLEAR")["outcome_evidence"] == \
            E.expected_track_record("site_a")


# ---------------------------------------------------------------------------
# 14. Case 20: the proposal's own outcome joins the CURRENT record, never the
#     creation record
# ---------------------------------------------------------------------------


class TestTheOperationAndItsOutcome:
    async def test_a_settled_outcome_counts_now_and_changes_nothing_recorded(self):
        """The exact execution key (A25.1: `directive:<id>`) links a proposal to
        its outcome row. Settling it moves the reader's CURRENT track record
        by exactly that row and leaves the creation record, the rationale and
        the lifecycle linkage as they were."""
        from harkeniq_cc.db.models import CCAgentProposal

        stack, ids = await E.scenario()
        subject = await E.persona(stack, "site_a")
        before = {i["proposal_id"]: i for i in await _queue(stack, subject, "site_admin")}
        creation_before = {k: before[ids["evaluated"]][k] for k in ("evidence", "rationale")}
        stored_before = await E.stored_bytes(stack)

        directive = "e2-directive-1"
        async with stack.sessionmaker() as session:
            session.add(CCOutcomeHistory(
                site_id=stack.site("A"), action_id=f"directive:{directive}",
                action_type="SEL_CLEAR", device_agent_id=stack.tagged("node-s3-a"),
                vendor="Dell", model="R750", outcome="SUCCESS", fault_resolved=True,
                actor="op-agent:x@v1", ingested_at=datetime.now(timezone.utc),
            ))
            await session.commit()

        after = {i["proposal_id"]: i for i in await _queue(stack, subject, "site_admin")}
        item = after[ids["evaluated"]]
        assert {k: item[k] for k in ("evidence", "rationale")} == creation_before
        assert _viewer(item)["outcome_evidence"]["executions"] == \
            _viewer(before[ids["evaluated"]])["outcome_evidence"]["executions"] + 1
        assert await E.stored_bytes(stack) == stored_before
        # The linkage itself is the settle pass's, read through the exact key
        # and never through a projection of evidence.
        async with stack.sessionmaker() as session:
            from harkeniq_cc.db.repos import OutcomeHistoryRepo

            row = await OutcomeHistoryRepo(session).find_by_action_id(
                stack.tenant, f"directive:{directive}")
            assert row is not None and row.outcome == "SUCCESS"
            stored = await session.get(CCAgentProposal, ids["evaluated"])
            assert stored.evidence["outcome_evidence"]["executions"] == 40
