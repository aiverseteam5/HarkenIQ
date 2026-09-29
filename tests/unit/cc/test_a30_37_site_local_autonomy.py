"""S3-E1 (A30.37): SITE-LOCAL AUTONOMY + CLOSED GLOBAL SAFETY GATE =
FINAL EXECUTION ELIGIBILITY -- the pure layer.

What this module proves, without a database:

* the registry (D1): EMPTY in production and reachable by no configuration
  (A30.38 -- the TEST-ONLY probe's activation boundary is proved in
  `test_a30_38_probe_activation_boundary.py`), a member that errors or answers
  anything but its enum FAILS CLOSED, and the verdict carries one bounded code
  and nothing else;
* monotonicity: over every local disposition and every gate state, the final
  eligibility is never above the local assessment -- a clear gate changes
  nothing and no member answer can grant;
* the site-local rules inside the one composer (R3, D8, D4) and the
  multi-site composite;
* HIDDEN-SITE NON-INTERFERENCE, the core proof: changing only another site
  leaves a site's local assessment byte-identical, and may change its global
  state and final eligibility only by narrowing, only through a member;
* admission (A-D of the brief's matrix): the mode follows the target's local
  assessment and never the gate (answer 1); D11 narrows; frozen evidence is
  unchanged (D7); and the pre-existing suppression-over-denial defect is
  closed (local rules only narrow);
* the bounded global row, rebuilt from constants for every reader.
"""

from __future__ import annotations

import ast
import inspect
import itertools
import json
import logging
import pathlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest

import harkeniq_cc
from harkeniq_cc import global_safety as G
from harkeniq_cc.autonomy import (
    AUTONOMOUS,
    DENIED,
    NO_SITE_REASON,
    NOT_BUDGET_MAPPED,
    REQUIRES_APPROVAL,
    build_autonomy,
    site_reported,
    visible_blocking_conditions,
    visible_disposition_reason,
)
from harkeniq_cc.governance import AutonomyInputs, SiteAssessments, gate_verdicts
from harkeniq_cc.operational_agent import (
    BASIS_AUTONOMOUS,
    BASIS_HUMAN,
    PROPOSAL_APPROVED,
    PROPOSAL_AWAITING,
    PROPOSAL_BLOCKED,
    effective_disposition,
    evaluate,
)

from tests.unit.cc.s3e1_support import clear_gate, fresh_report, gate_with

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
TENANT = "t-e1"
_RANK = {DENIED: 0, NOT_BUDGET_MAPPED: 1, REQUIRES_APPROVAL: 1, AUTONOMOUS: 2}


# ---------------------------------------------------------------------------
# Test members. None of these exists in production.
# ---------------------------------------------------------------------------


class Always:
    member_id = "test_always"

    def __init__(self, answer):
        self.answer = answer
        self.calls = 0

    def evaluate(self, context):
        self.calls += 1
        return self.answer


class Raises:
    member_id = "test_raises"

    def evaluate(self, context):
        raise RuntimeError("SECRET-MEMBER-FAILURE site-HIDDEN-9")


class HiddenHalt:
    """Constrains while ANY site in the estate is halted -- it READS hidden
    sites, which is the one ratified channel (A30.30(c)). Its only output is
    the enum."""

    member_id = "test_hidden_halt"

    def evaluate(self, context):
        if any(f.sm_stop_switch for f in context.estate):
            return G.MemberVerdict.CONSTRAIN
        return G.MemberVerdict.CLEAR


# ---------------------------------------------------------------------------
# The estate
# ---------------------------------------------------------------------------


def site(sid):
    return NS(id=sid, site_name=f"DC-{sid}")


def report(sid, **kw):
    return fresh_report(sid, now=NOW, **kw)


def budget(level=2):
    return NS(device_type="*", level=level, budget_limit=10,
              budget_period="daily", actions_used=0)


def compose(rows, sites, *, visible=None, gate=None, level=2, stop=False,
            action_type=None, outcomes=()):
    return build_autonomy(
        tenant_id=TENANT, actor_id="op-agent:e1@v1", actor_species="agent",
        permissions=["fleet.view"], budgets=[budget(level)],
        stop_switch=NS(active=stop, changed_by="", updated_at=NOW),
        outcomes=list(outcomes), safety_rows=list(rows), sites=list(sites),
        visible_site_ids=visible, now=NOW, action_type=action_type,
        global_safety=gate if gate is not None else clear_gate(),
    )


def klass(contract, action="SEL_CLEAR"):
    return next(c for c in contract["action_classes"] if c["action_type"] == action)


def inputs(rows, sites, *, level=2, stop=False, outcomes=()):
    return AutonomyInputs(
        tenant_id=TENANT, budgets=(budget(level),),
        stop_switch=NS(active=stop, changed_by="", updated_at=NOW),
        outcomes=tuple(outcomes), safety_rows=tuple(rows), sites=tuple(sites),
        learned=(), policies=(), now=NOW,
    )


def assessments(rows, sites, **kw):
    return SiteAssessments(
        inputs(rows, sites, **kw), actor_id="op-agent:e1@v1",
        actor_species="agent", permissions=["fleet.view"],
    )


def without_clock(contract):
    out = json.loads(json.dumps(contract, default=str))
    out.pop("generated_at", None)
    return out


# ---------------------------------------------------------------------------
# 1. The registry (D1)
# ---------------------------------------------------------------------------


class TestTheRegistry:
    def test_the_production_registry_is_empty(self):
        assert G.PRODUCTION_MEMBERS == ()

    def test_no_configuration_registers_a_member(self, monkeypatch):
        """A30.38 (inverted from A30.37's "the probe key is read from the
        environment", which pinned the defect): neither the environment nor
        the default configuration can register anything, and there is no
        `configure` left to ask. The full boundary -- YAML, real startup,
        fresh interpreter, structural guards -- is
        `test_a30_38_probe_activation_boundary.py`."""
        from harkeniq_cc.config import CCConfig, load_cc_config

        monkeypatch.setenv("HARKEN_CC_GLOBAL_SAFETY_TEST_PROBE", "/tmp/some-trigger")
        config = load_cc_config()
        assert not hasattr(config, "global_safety_test_probe")
        assert not hasattr(CCConfig(), "global_safety_test_probe")
        assert not hasattr(G, "configure")
        assert not hasattr(G, "TestOnlyProbeMember")
        assert G.active_members() == G.PRODUCTION_MEMBERS == ()

    def test_an_empty_registry_is_clear(self):
        gate = G.GlobalSafetyGate(members=(), tenant_id=TENANT, estate=())
        assert gate.verdict("SEL_CLEAR", "s1") is G.CLEAR

    def test_a_constraining_member_constrains(self):
        gate = G.GlobalSafetyGate(
            members=(Always(G.MemberVerdict.CONSTRAIN),), tenant_id=TENANT, estate=(),
        )
        assert gate.verdict("SEL_CLEAR").state == G.STATE_CONSTRAINED

    def test_a_member_that_raises_fails_closed(self):
        gate = G.GlobalSafetyGate(members=(Raises(),), tenant_id=TENANT, estate=())
        verdict = gate.verdict("SEL_CLEAR")
        assert verdict.state == G.STATE_UNKNOWN and not verdict.clear
        assert verdict.as_dict() == {
            "state": "unknown", "reason_codes": [G.GLOBAL_SAFETY_CONSTRAINT],
        }

    @pytest.mark.parametrize("answer", [True, False, None, "clear", "CLEAR", 1, 0,
                                        "constrain", G.CLEAR])
    def test_anything_but_the_enum_fails_closed(self, answer):
        gate = G.GlobalSafetyGate(members=(Always(answer),), tenant_id=TENANT, estate=())
        assert not gate.verdict("SEL_CLEAR").clear

    def test_constrained_outranks_unknown_and_both_fail_closed(self):
        gate = G.GlobalSafetyGate(
            members=(Raises(), Always(G.MemberVerdict.CONSTRAIN)),
            tenant_id=TENANT, estate=(),
        )
        assert gate.verdict("SEL_CLEAR").state == G.STATE_CONSTRAINED

    def test_each_question_is_asked_of_a_member_once(self):
        member = Always(G.MemberVerdict.CLEAR)
        gate = G.GlobalSafetyGate(members=(member,), tenant_id=TENANT, estate=())
        for _ in range(3):
            gate.verdict("SEL_CLEAR", "s1")
        gate.verdict("sel_clear", "s1")
        assert member.calls == 1
        gate.verdict("SEL_CLEAR", "s2")
        assert member.calls == 2

    def test_the_verdict_is_bounded_by_construction(self):
        for verdict in (G.CLEAR, G.CONSTRAINED, G.UNKNOWN):
            assert set(verdict.as_dict()) == {"state", "reason_codes"}
            assert set(verdict.as_dict()["reason_codes"]) <= {G.GLOBAL_SAFETY_CONSTRAINT}
        with pytest.raises(ValueError):
            G.GlobalSafetyVerdict("granted")

    def test_the_verdict_is_required_everywhere_it_is_read(self):
        with pytest.raises(TypeError, match="A30.37"):
            G.require_verdict(None)
        with pytest.raises(TypeError, match="A30.37"):
            build_autonomy(
                tenant_id="t", actor_id="a", actor_species="agent", permissions=[],
                budgets=[], stop_switch=None, outcomes=[], safety_rows=[], sites=[],
                global_safety=None,
            )
        assert inspect.signature(build_autonomy).parameters[
            "global_safety"].default is inspect.Parameter.empty


# ---------------------------------------------------------------------------
# 2. Monotonicity: the gate can only leave eligibility or lower it
# ---------------------------------------------------------------------------


LOCALS = (AUTONOMOUS, REQUIRES_APPROVAL, DENIED, NOT_BUDGET_MAPPED)
VERDICTS = (G.CLEAR, G.CONSTRAINED, G.UNKNOWN)


class TestMonotonicity:
    @pytest.mark.parametrize("local", LOCALS)
    @pytest.mark.parametrize("verdict", VERDICTS, ids=lambda v: v.state)
    def test_final_is_never_above_local(self, local, verdict):
        final = G.final_execution_eligibility(local, verdict)
        assert _RANK[final] <= _RANK[local]
        if verdict.clear:
            assert final == local
        else:
            assert final == DENIED

    @pytest.mark.parametrize("answer", list(G.MemberVerdict) + [True, None, "grant"])
    def test_no_member_answer_can_grant(self, answer):
        gate = G.GlobalSafetyGate(members=(Always(answer),), tenant_id=TENANT, estate=())
        for local in LOCALS:
            final = G.final_execution_eligibility(local, gate.verdict("SEL_CLEAR"))
            assert _RANK[final] <= _RANK[local]

    def test_the_gate_never_moves_the_local_disposition(self):
        """A constrained gate adds its bounded row and denies EXECUTION; the
        site-local answer -- the class `disposition` -- is untouched."""
        rows, sites = [report("s1")], [site("s1")]
        clear = compose(rows, sites)
        held = compose(rows, sites, gate=gate_with(
            Always(G.MemberVerdict.CONSTRAIN), safety_rows=rows, now=NOW,
        ))
        for a, b in zip(clear["action_classes"], held["action_classes"]):
            assert a["disposition"] == b["disposition"], a["action_type"]
            assert a["disposition_reason"] == b["disposition_reason"]
            assert b["final_execution_eligibility"]["disposition"] == DENIED
            assert b["global_safety"]["state"] == G.STATE_CONSTRAINED
            assert [r for r in b["blocking_conditions"] if r.get("scope") != "global"] \
                == a["blocking_conditions"]
            assert G.global_row() in b["blocking_conditions"]

    def test_the_empty_registry_neither_narrows_nor_widens(self):
        rows, sites = [report("s1"), report("s2", sm_stop_switch=True)], \
            [site("s1"), site("s2")]
        for row in compose(rows, sites)["action_classes"]:
            assert row["global_safety"] == {"state": "clear", "reason_codes": []}
            assert row["final_execution_eligibility"] == {
                "disposition": row["disposition"], "reason_codes": [],
            }


# ---------------------------------------------------------------------------
# 3. The site-local rules (R3, D8, D4) and the composite
# ---------------------------------------------------------------------------


class TestSiteLocalRules:
    def test_a_reporting_site_at_a_granting_level_is_autonomous(self):
        assert klass(compose([report("s1")], [site("s1")]))["disposition"] == AUTONOMOUS

    def test_no_report_requires_approval(self):
        row = klass(compose([], [site("s1")]))
        assert row["disposition"] == REQUIRES_APPROVAL
        assert {"code": "site_safety_not_reported", "scope": "site", "site_id": "s1"} \
            .items() <= row["blocking_conditions"][0].items()

    def test_a_report_marked_unreported_requires_approval(self):
        row = klass(compose([report("s1", reported=False)], [site("s1")]))
        assert row["disposition"] == REQUIRES_APPROVAL

    @pytest.mark.parametrize("age,expected", [
        (timedelta(minutes=14), AUTONOMOUS),
        (timedelta(minutes=15), AUTONOMOUS),
        (timedelta(minutes=15, seconds=1), REQUIRES_APPROVAL),
        (timedelta(hours=6), REQUIRES_APPROVAL),
    ])
    def test_the_one_freshness_window(self, age, expected):
        stamp = NOW - age
        row = klass(compose([report("s1", as_of=stamp, ingested_at=stamp)], [site("s1")]))
        assert row["disposition"] == expected

    def test_a_clock_running_ahead_cannot_keep_a_dark_site_fresh(self):
        """Judged on the OLDER of the site's reading time and CC's ingest."""
        future = NOW + timedelta(hours=3)
        stale_ingest = NOW - timedelta(hours=1)
        rows = [report("s1", as_of=future, ingested_at=stale_ingest)]
        assert not site_reported(rows[0], NOW)
        assert klass(compose(rows, [site("s1")]))["disposition"] == REQUIRES_APPROVAL

    def test_a_halted_site_is_a_local_denial(self):
        row = klass(compose([report("s1", sm_stop_switch=True)], [site("s1")]))
        assert row["disposition"] == DENIED
        assert any(r["code"] == "site_stop_switch_active" for r in row["blocking_conditions"])

    def test_a_stale_halt_still_denies(self):
        """Staleness narrows; it never lifts a restriction the report carried."""
        stamp = NOW - timedelta(days=2)
        rows = [report("s1", sm_stop_switch=True, as_of=stamp, ingested_at=stamp)]
        assert klass(compose(rows, [site("s1")]))["disposition"] == DENIED

    def test_a_stale_drop_back_still_restricts(self):
        stamp = NOW - timedelta(days=2)
        rows = [report("s1", as_of=stamp, ingested_at=stamp, error_budgets=[{
            "action_type": "SEL_CLEAR", "dropped_back": True, "total_count": 9,
            "success_count": 1, "failure_count": 8}])]
        row = klass(compose(rows, [site("s1")]))
        assert row["disposition"] == REQUIRES_APPROVAL
        assert any(r["code"] == "error_budget_dropped_back" for r in row["blocking_conditions"])

    def test_tenant_level_conditions_are_unchanged(self):
        rows, sites = [report("s1")], [site("s1")]
        assert klass(compose(rows, sites, stop=True))["disposition"] == DENIED
        assert klass(compose(rows, sites, level=1))["disposition"] == REQUIRES_APPROVAL
        assert klass(compose(rows, sites), "FIRMWARE_UPDATE")["disposition"] == DENIED
        assert klass(compose(rows, sites), "IDENTIFY_LED")["disposition"] == NOT_BUDGET_MAPPED

    def test_a_halt_under_a_low_level_is_still_a_denial(self):
        row = klass(compose([report("s1", sm_stop_switch=True)], [site("s1")], level=1))
        assert row["disposition"] == DENIED

    @pytest.mark.parametrize("second,expected", [
        (dict(), AUTONOMOUS),
        (dict(sm_stop_switch=True), REQUIRES_APPROVAL),
        (dict(reported=False), REQUIRES_APPROVAL),
        (dict(site_budgets={"SEL_CLEAR": 0}), REQUIRES_APPROVAL),
    ])
    def test_the_composite_needs_every_site(self, second, expected):
        rows = [report("s1"), report("s2", **second)]
        assert klass(compose(rows, [site("s1"), site("s2")]))["disposition"] == expected

    def test_every_site_denied_is_a_denial(self):
        rows = [report("s1", sm_stop_switch=True), report("s2", sm_stop_switch=True)]
        assert klass(compose(rows, [site("s1"), site("s2")]))["disposition"] == DENIED

    def test_a_selection_with_no_site_requires_approval_and_names_nothing(self):
        contract = compose([report("s1")], [site("s1")], visible=set())
        row = klass(contract)
        assert row["disposition"] == REQUIRES_APPROVAL
        assert row["disposition_reason"] == NO_SITE_REASON
        assert row["blocking_conditions"] == []
        assert contract["safety_state"]["every_site_reported"] is False

    def test_every_site_reported(self):
        both = compose([report("s1"), report("s2")], [site("s1"), site("s2")])
        one = compose([report("s1")], [site("s1"), site("s2")])
        assert both["safety_state"]["every_site_reported"] is True
        assert one["safety_state"]["every_site_reported"] is False
        assert one["safety_state"]["reported"] is True        # ANY, unchanged


# ---------------------------------------------------------------------------
# 4. Hidden-site non-interference -- the core proof
# ---------------------------------------------------------------------------


#: Every way site B alone can change. Each is applied to B and ONLY to B.
HIDDEN_CHANGES = {
    "halted": dict(sm_stop_switch=True),
    "unreported": dict(reported=False),
    "stale": dict(as_of=NOW - timedelta(days=1), ingested_at=NOW - timedelta(days=1)),
    "dropped_back": dict(error_budgets=[{
        "action_type": "SEL_CLEAR", "dropped_back": True, "total_count": 97531,
        "success_count": 1, "failure_count": 97530}]),
    "exhausted": dict(site_budgets={"SEL_CLEAR": 0, "BMC_RESET": 0}),
    "suppressed": dict(suppressions=[{
        "domain_id": "SECRET-DOMAIN-B", "trigger_reason": "SECRET-REASON-B",
        "device_count": 86420}]),
}


class TestHiddenSiteNonInterference:
    @pytest.mark.parametrize("change", sorted(HIDDEN_CHANGES))
    def test_a_sites_local_assessment_is_byte_identical(self, change):
        sites = [site("A"), site("B")]
        before = assessments([report("A"), report("B")], sites)
        after = assessments([report("A"), report("B", **HIDDEN_CHANGES[change])], sites)
        assert without_clock(before.local_contract(("A",))) == \
            without_clock(after.local_contract(("A",))), change
        # Non-vacuity: B's own local assessment DID move.
        assert without_clock(before.local_contract(("B",))) != \
            without_clock(after.local_contract(("B",))), change

    @pytest.mark.parametrize("change", sorted(HIDDEN_CHANGES))
    def test_deletion_equivalence(self, change):
        """A's local assessment over the whole estate IS a tenant-wide
        composition of an estate where B does not exist."""
        full = assessments([report("A"), report("B", **HIDDEN_CHANGES[change])],
                           [site("A"), site("B")])
        alone = compose([report("A")], [site("A")])
        assert without_clock(full.local_contract(("A",)))["action_classes"] == \
            without_clock(alone)["action_classes"]

    @pytest.mark.parametrize("change", sorted(HIDDEN_CHANGES))
    def test_nothing_of_b_reaches_as_local_contract(self, change):
        full = assessments([report("A"), report("B", **HIDDEN_CHANGES[change])],
                           [site("A"), site("B")])
        text = json.dumps(full.local_contract(("A",)), default=str)
        for sentinel in ('"B"', "SECRET", "97531", "86420"):
            assert sentinel not in text, (change, sentinel)

    def test_a_hidden_site_reaches_eligibility_only_through_a_member(self, monkeypatch):
        """The ONE ratified channel: a member that reads B's halt constrains
        the gate for A. A's local assessment is untouched, the final
        eligibility only narrows, and nothing names B."""
        sites = [site("A"), site("B")]
        monkeypatch.setattr(G, "_ACTIVE", (HiddenHalt(),))
        calm = assessments([report("A"), report("B")], sites)
        halted = assessments([report("A"), report("B", sm_stop_switch=True)], sites)
        before, after = calm.local_contract(("A",)), halted.local_contract(("A",))
        for a, b in zip(before["action_classes"], after["action_classes"]):
            assert a["disposition"] == b["disposition"]
            assert a["global_safety"]["state"] == "clear"
            assert b["global_safety"]["state"] == "constrained"
            assert _RANK[b["final_execution_eligibility"]["disposition"]] <= \
                _RANK[a["final_execution_eligibility"]["disposition"]]
        text = json.dumps(after, default=str)
        assert '"B"' not in text and "DC-B" not in text
        assert "test_hidden_halt" not in text           # no member id, ever

    def test_the_old_fold_would_have_moved_a(self):
        """CONTROL: composed over BOTH sites (a reader of the whole tenant),
        B's drop-back DOES appear -- so the local proof above is not
        vacuous. S3-E1 made a decision read the local one."""
        both = compose([report("A"), report("B", **HIDDEN_CHANGES["dropped_back"])],
                       [site("A"), site("B")])
        assert klass(both)["disposition"] == REQUIRES_APPROVAL


# ---------------------------------------------------------------------------
# 5. Admission: the mode is the target site's, never the gate's
# ---------------------------------------------------------------------------


def _agent(**kw):
    base = dict(id="ag", tenant_id=TENANT, name="Night", status="active", version=1,
                autonomy_ceiling=3, require_approval_always=False,
                max_proposals_per_day=25)
    base.update(kw)
    return NS(**base)


def _device(sid="A"):
    return NS(agent_id=f"node-{sid}", agent_name=f"node-{sid}", site_id=sid,
              device_class="server", health="Critical", observation="observed",
              vendor="Dell", model="R750", capabilities=None)


def _catalogue():
    from harkeniq_cc.capability_catalogue import SEED

    out: dict = {}
    for entry in SEED:
        out.setdefault(entry["subsystem"], []).append({
            "action_type": entry["action_type"], "because": entry["because"],
            "provenance": entry["provenance"],
        })
    return out


APPROVED = frozenset({"SEL_CLEAR", "BMC_RESET"})


def _propose(estate, *, sid="A", agent=None, approved=APPROVED, action="SEL_CLEAR",
             subsystem="log", components=()):
    (proposal,) = evaluate(
        catalogue=_catalogue(), agent=agent or _agent(),
        scopes=[NS(scope_type="site", scope_ref=sid)],
        capabilities=[NS(kind="action_class", capability_ref=action)],
        devices=[_device(sid)],
        incidents_by_device={f"node-{sid}": [
            {"incident_id": f"i-{sid}", "subsystem": subsystem, "title": "fault",
             "components": [{"component": c, "severity": "CRITICAL"}
                            for c in components]},
        ]},
        assessments=estate, unattended_approved=approved, now=NOW,
    )
    return proposal


class TestAdmission:
    SITES = [site("A"), site("B")]

    def test_A_visible_safe_hidden_safe_is_autonomous(self):
        p = _propose(assessments([report("A"), report("B")], self.SITES))
        assert (p["status"], p["authorization_basis"]) == (PROPOSAL_APPROVED, BASIS_AUTONOMOUS)

    def test_A_hidden_drop_back_no_longer_routes_to_a_human(self):
        """THE fix (Model C). Under the tenant-wide fold this proposal
        waited for a human because site B had dropped back."""
        estate = assessments(
            [report("A"), report("B", **HIDDEN_CHANGES["dropped_back"])], self.SITES,
        )
        p = _propose(estate)
        assert p["status"] == PROPOSAL_APPROVED
        assert {r.get("site_id") for r in p["blocking_conditions"] if r.get("site_id")} <= {"A"}

    def test_B_a_global_constraint_holds_execution_not_mode(self, monkeypatch):
        # The gate is built with the estate, so the member is registered first.
        monkeypatch.setattr(G, "_ACTIVE", (Always(G.MemberVerdict.CONSTRAIN),))
        estate = assessments([report("A"), report("B")], self.SITES)
        p = _propose(estate)
        assert (p["status"], p["authorization_basis"]) == (PROPOSAL_APPROVED, BASIS_AUTONOMOUS)
        assert p["disposition"] == AUTONOMOUS
        assert G.global_row() in p["blocking_conditions"]
        assert sum(1 for r in p["blocking_conditions"] if r.get("scope") == "global") == 1

    def test_C_visible_locally_unsafe_requires_approval(self):
        estate = assessments([report("A", site_budgets={"SEL_CLEAR": 0}), report("B")],
                             self.SITES)
        p = _propose(estate)
        assert (p["status"], p["authorization_basis"]) == (PROPOSAL_AWAITING, BASIS_HUMAN)

    def test_D_locally_and_globally_unsafe(self, monkeypatch):
        monkeypatch.setattr(G, "_ACTIVE", (Always(G.MemberVerdict.CONSTRAIN),))
        estate = assessments([report("A", reported=False), report("B")], self.SITES)
        p = _propose(estate)
        assert p["status"] == PROPOSAL_AWAITING
        assert G.global_row() in p["blocking_conditions"]

    def test_a_halted_target_is_blocked(self):
        estate = assessments([report("A", sm_stop_switch=True), report("B")], self.SITES)
        assert _propose(estate)["status"] == PROPOSAL_BLOCKED

    def test_D11_a_class_activation_did_not_approve_needs_a_human(self):
        estate = assessments([report("A"), report("B")], self.SITES)
        p = _propose(estate, approved=frozenset())
        assert p["status"] == PROPOSAL_AWAITING
        assert any(r["code"] == "agent_unattended_not_approved"
                   for r in p["blocking_conditions"])

    def test_D11_is_required(self):
        row = klass(compose([report("A")], [site("A")]))
        with pytest.raises(TypeError, match="D11"):
            effective_disposition(_agent(), row, unattended_approved=None)

    def test_a_suppression_never_turns_a_denial_into_an_approval(self):
        """The pre-existing defect S3-E1 found: a suppression at the target
        site set REQUIRES_APPROVAL unconditionally, so a DENIED class became
        approvable by a human. It narrows only."""
        suppressed = report("A", suppressions=[{"domain_id": "d", "trigger_reason": "r"}])
        estate = assessments([suppressed], [site("A")])
        fenced = _propose(estate, action="INTERFACE_DISABLE", subsystem="interface",
                          components=("Ethernet0",))
        assert fenced["status"] == PROPOSAL_BLOCKED
        stopped = assessments([suppressed], [site("A")], stop=True)
        assert _propose(stopped)["status"] == PROPOSAL_BLOCKED
        # ...and it still narrows autonomy, as before.
        assert _propose(estate)["status"] == PROPOSAL_AWAITING

    def test_D7_frozen_evidence_is_unchanged(self):
        """The evidence a proposal freezes is still the whole-tenant row's
        (S3-E2 owns it), while the DECISION is site A's."""
        outcomes = (
            [{"action_type": "SEL_CLEAR", "outcome": "SUCCESS", "site_id": "A"}] * 3
            + [{"action_type": "SEL_CLEAR", "outcome": "SUCCESS", "site_id": "B"}] * 5
        )
        estate = assessments([report("A"), report("B")], self.SITES, outcomes=outcomes)
        p = _propose(estate)
        assert p["evidence"]["outcome_evidence"]["executions"] == 8
        assert "8 executions in this tenant" in p["rationale"]


# ---------------------------------------------------------------------------
# 6. The bounded global row, for every reader
# ---------------------------------------------------------------------------


class TestTheBoundedRow:
    TAMPERED = {"code": "GLOBAL_SAFETY_CONSTRAINT", "scope": "global",
                "detail": "SECRET site-HIDDEN-9 has 97531 failures",
                "site_id": "site-HIDDEN-9", "member_id": "test_hidden_halt",
                "count": 97531}

    @pytest.mark.parametrize("reader", [None, set(), {"A"}, {"A", "B"}],
                             ids=["tenant", "nothing", "site", "two-sites"])
    def test_every_reader_gets_exactly_the_constant_row(self, reader):
        rows = visible_blocking_conditions([self.TAMPERED], reader)
        assert rows == [G.global_row()]
        assert rows[0] == {"code": G.GLOBAL_SAFETY_CONSTRAINT,
                           "detail": G.GLOBAL_ROW_DETAIL, "scope": "global"}

    def test_a_reason_that_repeats_a_tampered_row_is_withheld(self):
        reason = visible_disposition_reason(
            self.TAMPERED["detail"], [self.TAMPERED], {"A"},
        )
        assert "HIDDEN" not in reason and "97531" not in reason

    def test_the_row_is_built_from_constants_only(self):
        tree = ast.parse(inspect.getsource(G.global_row))
        (ret,) = [n for n in ast.walk(tree) if isinstance(n, ast.Return)]
        assert isinstance(ret.value, ast.Dict)
        for value in ret.value.values:
            assert isinstance(value, (ast.Name, ast.Constant)), ast.dump(value)

    def test_the_withheld_reasons_are_constants(self):
        from harkeniq_cc.agent_activation import (
            SITE_LOCAL_WITHHELD_REASON,
            UNATTENDED_NOT_APPROVED_REASON,
        )

        for text in (G.GLOBAL_WITHHELD_REASON, SITE_LOCAL_WITHHELD_REASON,
                     UNATTENDED_NOT_APPROVED_REASON):
            assert isinstance(text, str) and "{" not in text

    def test_a_withheld_reason_promises_nothing_about_what_happens_next(self):
        """The synchronous approval path records the SAME reason on a TERMINAL
        failure (A30.17, D6 preserved; F-12), while the background pass keeps
        the proposal approved and retries it. A reason is true on both only
        if it states what holds -- "resumes when it clears" was recorded on
        failed proposals that never would."""
        from harkeniq_cc.agent_activation import (
            SITE_LOCAL_WITHHELD_REASON,
            UNATTENDED_NOT_APPROVED_REASON,
        )

        for text in (G.GLOBAL_WITHHELD_REASON, SITE_LOCAL_WITHHELD_REASON,
                     UNATTENDED_NOT_APPROVED_REASON):
            lowered = text.lower()
            for promise in ("resume", "stands", "retr", "will ", "until"):
                assert promise not in lowered, (promise, text)


# ---------------------------------------------------------------------------
# 7. The shape that keeps it fixed
# ---------------------------------------------------------------------------


PACKAGE = pathlib.Path(harkeniq_cc.__file__).parent


def _callers(name: str) -> set:
    out = set()
    for path in sorted(PACKAGE.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call):
                func = node.func
                called = getattr(func, "id", None) or getattr(func, "attr", None)
                if called == name:
                    out.add(str(path.relative_to(PACKAGE)))
    return out


class TestTheShape:
    def test_the_gate_is_built_in_one_place(self):
        """Every decision path reads the gate the one loader builds from the
        registry; a second construction could skip members."""
        assert _callers("GlobalSafetyGate") == {"governance.py"}
        assert _callers("active_members") == {"governance.py"}

    def test_startup_announces_the_registry_and_never_configures_it(self):
        """A30.38: startup READS the registry to say which one it serves
        with; nothing in the package configures it."""
        assert _callers("announce_registry") == {"runtime.py"}
        assert _callers("configure") & {"runtime.py", "global_safety.py"} == set()
        tree = ast.parse((PACKAGE / "runtime.py").read_text())
        touched = {
            n.attr for n in ast.walk(tree)
            if isinstance(n, ast.Attribute)
            and getattr(n.value, "id", "") == "global_safety"
        }
        assert touched == {"announce_registry"}, touched

    def test_the_dispatch_gates_follow_every_existing_gate(self):
        from harkeniq_cc.agent_activation import DISPATCH_GATES

        assert DISPATCH_GATES == (
            "agent_identity", "agent_active", "tenant_scope", "stop_switch",
            "budget", "effective_scope", "capability_binding",
            "unattended_class", "site_local_autonomy", "global_safety",
        )

    def test_an_unsupplied_e1_gate_refuses(self):
        from harkeniq_cc.agent_activation import dispatch_permitted

        existing = dict.fromkeys((
            "agent_identity", "agent_active", "tenant_scope", "stop_switch",
            "budget", "effective_scope", "capability_binding",
            "unattended_class", "site_local_autonomy",
        ), True)
        ok, why = dispatch_permitted(**existing)
        assert not ok and "global_safety was never evaluated" in why

    def test_the_gate_never_decides_a_mode(self):
        """Answer 1, structurally: `govern_proposal` reads the gate only to
        record its row -- its status comes from the disposition alone."""
        from harkeniq_cc import operational_agent

        src = inspect.getsource(operational_agent.govern_proposal)
        status_block = src[src.index("if disposition == DENIED:"):
                           src.index("attention = attention or {}")]
        assert "gate" not in status_block
