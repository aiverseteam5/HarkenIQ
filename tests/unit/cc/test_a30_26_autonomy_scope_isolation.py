"""A6-4B0b-S3 (spec A30.26): autonomy scope isolation.

P2, reproduced on `main` before this slice: `build_autonomy` folded EVERY
site's safety state and `narrow_to_sites` then dropped list items whose
`site_id` was outside the reader's reach. An aggregate has no `site_id`,
so a site-A reader received the tenant's error-budget totals with
`sites_dropped_back: [site-C]`, every site's suppressed fault domains, a
budget map keyed by every site id, a `reported` flag and a stop-switch
count that included hidden sites, and a DISPOSITION that read
`requires_approval` because a site they could not see had dropped back.
Two more faces came out of the inventory: `?site_id=` was a probe that
returned any site in isolation, and the evaluator's stored blocking
conditions carried hidden sites through seven proposal projections.

    For any autonomy projection returned to principal P, EVERY
    site-derived fact is derived only from sites covered by P's current
    canonical effective scope.

How that is proven here:

* **Deletion equivalence** -- the invariant, stated as a test. A scoped
  reader of the full estate must receive exactly what a tenant-wide
  reader receives over an estate in which the hidden sites DO NOT EXIST.
  The oracle is the composer itself run on less data, so it cannot share
  a mistake about which field is an aggregate.
* **Poisoned rows** -- filter BEFORE aggregate, shown by behaviour: hidden
  rows raise if anything but their site id is read. The same rows under a
  tenant-wide reader DO raise, which is the control.
* **Sentinels** -- `s3_estate`: magnitude encodes provenance, so a leak
  in a field nobody listed is still found.
* **Every narrowing has a control** that reads the same fact, so an empty
  answer cannot pass as a correct one.
"""

from __future__ import annotations

import ast
import inspect
import itertools
import pathlib

import pytest

import harkeniq_cc
from harkeniq_cc import agent_runtime
from harkeniq_cc.api import approvals as approvals_api
from harkeniq_cc.autonomy import (
    AUTONOMOUS,
    REQUIRES_APPROVAL,
    WITHHELD_REASON,
    build_autonomy,
    select_site_inputs,
    visible_blocking_conditions,
    visible_disposition_reason,
    visible_learned_signals,
    visible_verdict_evidence,
)
from harkeniq_cc.governance import (
    AUTONOMY_FACT_PERMISSION,
    AutonomyView,
    authorized_sites,
    autonomy_view,
    load_autonomy_contract,
    require_autonomy_view,
)
from harkeniq_cc.scope import empty_scope, read_reach

from tests.unit.cc import s3_estate as E
from tests.unit.cc.s3_estate import ALL, SITES, TENANT

SUBSETS = [
    keys for n in range(len(ALL) + 1) for keys in itertools.combinations(ALL, n)
]


def ids(keys) -> frozenset:
    return frozenset(SITES[k].id for k in keys)


def compose(keys=ALL, **kw) -> dict:
    return build_autonomy(**{**E.pure_inputs(keys), **kw})


def klass(contract: dict, action: str = "SEL_CLEAR") -> dict:
    return next(c for c in contract["action_classes"] if c["action_type"] == action)


# ---------------------------------------------------------------------------
# 1. The invariant, as deletion equivalence
# ---------------------------------------------------------------------------


class TestAHiddenSiteHasNoInfluence:
    @pytest.mark.parametrize("holds", SUBSETS, ids=lambda k: "".join(k) or "none")
    def test_a_scoped_reader_reads_the_estate_without_the_hidden_sites(self, holds):
        """All 16 subsets of four sites. If ANY field of the contract --
        a total, a boolean, a disposition, a sentence -- were computed
        from a hidden site, these two would differ."""
        scoped = compose(ALL, visible_site_ids=ids(holds))
        deleted = compose(holds)
        assert scoped == deleted

    @pytest.mark.parametrize("holds", SUBSETS, ids=lambda k: "".join(k) or "none")
    def test_nothing_in_the_payload_came_from_a_hidden_site(self, holds):
        """The sentinel walk: no marker, no key and no number in a hidden
        site's decade band, anywhere in the contract."""
        assert E.leaks(compose(ALL, visible_site_ids=ids(holds)), holds) == []

    def test_the_sentinels_are_live(self):
        """CONTROL. The walk finds every hidden site when the narrowing is
        absent -- otherwise an estate with nothing in it would pass."""
        found = " | ".join(E.leaks(compose(ALL), holds=("A",)))
        for marker in ("SECRET-C", "s3-site-c", "fault-B", "s3-site-b",
                       "site C: number", "site B: number"):
            assert marker in found, marker

    @pytest.mark.parametrize("holds", SUBSETS, ids=lambda k: "".join(k) or "none")
    def test_every_aggregate_is_recomputed_from_the_authorized_subset(self, holds):
        """Against an oracle written from the estate definition, not from
        the composer: select the authorized inputs, THEN aggregate."""
        contract = compose(ALL, visible_site_ids=ids(holds))
        row = klass(contract)
        expected = E.expected_error_budget(holds)
        assert row["safety"]["error_budget"] == expected
        top = [e for e in contract["safety_state"]["error_budgets"]
               if e["action_type"] == "SEL_CLEAR"]
        assert top == ([{"action_type": "SEL_CLEAR", **expected}] if expected else [])
        assert row["safety"]["site_budget_remaining"] == E.expected_remaining(holds)
        assert [d["domain_id"] for d in row["safety"]["suppressed_domains"]] \
            == E.expected_domains(holds)
        assert [s["domain_id"] for s in contract["safety_state"]["suppressions"]] \
            == E.expected_domains(holds)
        assert row["evidence"]["executions"] == E.expected_executions(holds)
        assert contract["safety_state"]["sites_reporting"] == E.expected_reporting(holds)
        assert contract["safety_state"]["reported"] is bool(E.expected_reporting(holds))
        assert row["safety"]["reported"] is bool(E.expected_reporting(holds))
        assert [s["id"] for s in contract["scope"]["sites"]] \
            == [SITES[k].id for k in holds]
        assert contract["posture"]["stop_switch"]["sites_reporting_active"] \
            == (1 if "C" in holds else 0)

    def test_the_example_in_the_ratification(self):
        """Budgets 11 / 23 / 47: "if 47 influences any returned total or
        state: FAIL". Written out longhand, with nothing shared with the
        estate above."""
        from types import SimpleNamespace as NS

        def row(site, remaining, total, failures, dropped, domain):
            return NS(
                site_id=site, reported=True, as_of=None, sm_stop_switch=False,
                suppressions=[{"domain_id": domain}],
                error_budgets=[{"action_type": "SEL_CLEAR", "total_count": total,
                                "success_count": total - failures,
                                "failure_count": failures, "dropped_back": dropped}],
                site_budgets={"SEL_CLEAR": remaining},
            )

        rows = [row("site-A", 11, 11, 0, False, "fault-A"),
                row("site-B", 23, 23, 1, False, "fault-B"),
                row("site-C", 47, 47, 40, True, "SECRET-C")]
        contract = build_autonomy(
            tenant_id=TENANT, actor_id="u", actor_species="human",
            permissions=["fleet.view"],
            budgets=[NS(device_type="*", level=2, budget_limit=10,
                        budget_period="daily", actions_used=0)],
            stop_switch=None, outcomes=[], safety_rows=rows,
            sites=[NS(id=r.site_id, site_name=r.site_id) for r in rows],
            visible_site_ids={"site-A", "site-B"},
        )
        sel = klass(contract)
        assert sel["safety"]["error_budget"]["total"] == 34          # not 81
        assert sel["safety"]["error_budget"]["failure"] == 1         # not 41
        assert sel["safety"]["error_budget"]["dropped_back"] is False
        assert sel["safety"]["error_budget"]["sites_dropped_back"] == []
        assert sel["safety"]["site_budget_remaining"] == {"site-A": 11, "site-B": 23}
        assert sel["disposition"] == AUTONOMOUS
        assert 47 not in set(E.leaves(contract))
        assert not [leaf for leaf in E.leaves(contract)
                    if isinstance(leaf, str) and ("SECRET-C" in leaf or "site-C" in leaf)]


# ---------------------------------------------------------------------------
# 2. Filter BEFORE aggregate -- by behaviour, not by reading the source
# ---------------------------------------------------------------------------


class _Poisoned:
    """A hidden site's row. Its site id may be read; nothing else may."""

    def __init__(self, **readable):
        object.__setattr__(self, "_readable", readable)

    def __getattr__(self, name):
        readable = object.__getattribute__(self, "_readable")
        if name in readable:
            return readable[name]
        raise AssertionError(f"a hidden site's row was read: .{name}")


class _PoisonedOutcome(dict):
    def get(self, key, default=None):
        if key != "site_id":
            raise AssertionError(f"a hidden site's outcome was read: [{key!r}]")
        return super().get(key, default)

    def __getitem__(self, key):
        if key != "site_id":
            raise AssertionError(f"a hidden site's outcome was read: [{key!r}]")
        return super().__getitem__(key)


def _poison(inputs: dict, hidden) -> dict:
    hidden_ids = ids(hidden)
    out = dict(inputs)
    out["safety_rows"] = [
        _Poisoned(site_id=r.site_id) if r.site_id in hidden_ids else r
        for r in inputs["safety_rows"]
    ]
    out["sites"] = [
        _Poisoned(id=s.id) if s.id in hidden_ids else s for s in inputs["sites"]
    ]
    out["outcomes"] = [
        _PoisonedOutcome(site_id=o["site_id"]) if o["site_id"] in hidden_ids else o
        for o in inputs["outcomes"]
    ]
    out["learned_signals"] = [
        _Poisoned(scope_type=s.scope_type, scope_ref=s.scope_ref)
        if s.scope_type == "site" and s.scope_ref in hidden_ids else s
        for s in inputs["learned_signals"]
    ]
    return out


class TestHiddenRowsNeverReachTheFold:
    @pytest.mark.parametrize("holds", [("A", "B"), ("A",), ("B",), ("D",), ()],
                             ids=lambda k: "".join(k) or "none")
    def test_a_narrowed_composition_reads_only_a_hidden_rows_site_id(self, holds):
        hidden = [k for k in ALL if k not in holds]
        poisoned = _poison(E.pure_inputs(ALL), hidden)
        contract = build_autonomy(**poisoned, visible_site_ids=ids(holds))
        assert contract == compose(holds)

    def test_the_poison_is_live(self):
        """CONTROL. The identical rows, composed for a tenant-wide reader,
        DO get read -- so the test above passes because the rows were
        filtered out first, not because nothing looks at rows."""
        poisoned = _poison(E.pure_inputs(ALL), ["C"])
        with pytest.raises(AssertionError, match="a hidden site's"):
            build_autonomy(**poisoned)

    def test_selection_is_the_first_thing_that_happens_and_is_total(self):
        """`select_site_inputs` returns the four lists the fold uses, and
        an unselected row is in none of them."""
        inputs = E.pure_inputs(ALL)
        safety, sites, outcomes, signals = select_site_inputs(
            safety_rows=inputs["safety_rows"], sites=inputs["sites"],
            outcomes=inputs["outcomes"], learned_signals=inputs["learned_signals"],
            visible_site_ids=ids(("A", "B")),
        )
        allowed = ids(("A", "B"))
        assert {r.site_id for r in safety} == allowed
        assert {s.id for s in sites} == allowed
        assert {o["site_id"] for o in outcomes} == allowed
        assert sorted(s.statement for s in signals) == [
            "cohort-knowledge", "signal-A", "signal-B",
        ]

    def test_an_empty_set_selects_nothing_and_is_never_unrestricted(self):
        """`frozenset()` is a reader who holds no site. Reading it as
        "no restriction" is the classic way this kind of filter fails."""
        for empty in (frozenset(), set(), [], ()):
            contract = compose(ALL, visible_site_ids=empty)
            assert contract["scope"]["sites"] == []
            assert contract["safety_state"]["error_budgets"] == []
            assert contract["safety_state"]["reported"] is False
            assert E.leaks(contract, holds=()) == []

    def test_none_is_the_whole_tenant_and_changes_nothing(self):
        assert compose(ALL, visible_site_ids=None) == compose(ALL)


# ---------------------------------------------------------------------------
# 3. No hidden site flips a boolean, a disposition or a sentence
# ---------------------------------------------------------------------------


class TestAHiddenSiteNeverChangesAnAnswer:
    def test_a_drop_back_at_a_hidden_site_does_not_withdraw_the_class(self):
        """The defect's sharpest form: the disposition itself."""
        everything, ab = compose(ALL), compose(ALL, visible_site_ids=ids(("A", "B")))
        assert klass(everything)["disposition"] == REQUIRES_APPROVAL    # control
        assert klass(everything)["approval"]["required"] is True
        assert klass(ab)["disposition"] == AUTONOMOUS
        assert klass(ab)["approval"]["required"] is False
        assert "error budget" not in klass(ab)["disposition_reason"]
        assert klass(ab)["advancement"]["gate"] == "granted"
        assert klass(everything)["advancement"]["gate"] == "operator_review"

    def test_a_spent_budget_at_a_hidden_site_does_not_demand_approval(self):
        """BMC_RESET is spent at C and nowhere else: a hidden site that
        changes an answer without contributing one large number."""
        everything, ab = compose(ALL), compose(ALL, visible_site_ids=ids(("A", "B")))
        codes = [b["code"] for b in klass(everything, "BMC_RESET")["blocking_conditions"]]
        assert "budget_window_exhausted" in codes                       # control
        assert klass(everything, "BMC_RESET")["disposition"] == REQUIRES_APPROVAL
        assert klass(ab, "BMC_RESET")["disposition"] == AUTONOMOUS
        assert klass(ab, "BMC_RESET")["evidence"]["executions"] == 0    # C's six

    def test_a_hidden_site_reporting_does_not_make_safety_reported(self):
        """A reader who holds only the site that never reported is told
        UNKNOWN, whatever the rest of the tenant has reported."""
        only_d = compose(ALL, visible_site_ids=ids(("D",)))
        assert compose(ALL)["safety_state"]["reported"] is True         # control
        assert only_d["safety_state"]["reported"] is False
        assert klass(only_d)["safety"]["reported"] is False
        assert only_d["safety_state"]["sites_not_reporting"] == [SITES["D"].id]

    def test_a_hidden_stop_switch_is_not_counted(self):
        assert compose(ALL)["posture"]["stop_switch"]["sites_reporting_active"] == 1
        ab = compose(ALL, visible_site_ids=ids(("A", "B")))
        assert ab["posture"]["stop_switch"]["sites_reporting_active"] == 0
        assert ab["safety_state"]["site_stop_switches"] == []

    def test_tenant_owned_posture_is_the_same_for_every_reader(self):
        """What is genuinely tenant-owned is preserved -- for a reader who
        holds nothing, too."""
        full = compose(ALL)
        for holds in [("A", "B"), ("A",), ()]:
            scoped = compose(ALL, visible_site_ids=ids(holds))
            for key in ("configured_level", "level_source", "budget_limit",
                        "budget_period", "actions_used", "ladder",
                        "device_scoped_budgets"):
                assert scoped["posture"][key] == full["posture"][key], key
            assert scoped["posture"]["stop_switch"]["active"] \
                is full["posture"]["stop_switch"]["active"]
            for a, b in zip(scoped["action_classes"], full["action_classes"]):
                for key in ("action_type", "risk", "required_permission",
                            "granted_at_level", "budget_mapped",
                            "never_budget_grantable"):
                    assert a[key] == b[key], (holds, key)
                assert {k: a["approval"][k] for k in ("mode", "required_approvers", "policy_id")} \
                    == {k: b["approval"][k] for k in ("mode", "required_approvers", "policy_id")}
                assert [c for c in a["blocking_conditions"] if c["scope"] == "tenant"] \
                    == [c for c in b["blocking_conditions"] if c["scope"] == "tenant"]


# ---------------------------------------------------------------------------
# 4. `site_id` narrows inside the selection and is not an oracle
# ---------------------------------------------------------------------------


class TestTheSiteIdProbe:
    def test_a_site_outside_the_reach_composes_over_nothing(self):
        held = ids(("A",))
        probe = compose(ALL, visible_site_ids=held, site_id=SITES["C"].id)
        # The one place site C's id may appear: the caller's own question,
        # echoed back. Everything else must be empty.
        assert probe["scope"].pop("site_id") == SITES["C"].id
        assert E.leaks(probe, holds=("A",)) == []
        assert probe["scope"]["sites"] == []
        assert probe["safety_state"]["reported"] is False
        assert probe["safety_state"]["error_budgets"] == []
        assert probe["safety_state"]["suppressions"] == []
        assert klass(probe)["safety"]["error_budget"] is None
        assert klass(probe)["safety"]["suppressed_domains"] == []
        assert klass(probe)["safety"]["site_budget_remaining"] == {}
        assert klass(probe)["evidence"]["executions"] == 0

    def test_and_is_indistinguishable_from_a_site_that_does_not_exist(self):
        held = ids(("A",))
        hidden = compose(ALL, visible_site_ids=held, site_id=SITES["C"].id)
        absent = compose(ALL, visible_site_ids=held, site_id="s3-no-such-site")
        hidden["scope"]["site_id"] = absent["scope"]["site_id"] = "<echo>"
        assert hidden == absent

    def test_the_probe_was_real(self):
        """CONTROL. A tenant-wide reader asking for site C gets site C in
        isolation -- which is what a site-A reader got before."""
        probe = compose(ALL, site_id=SITES["C"].id)
        assert klass(probe)["safety"]["error_budget"]["total"] == 90_000
        assert [d["domain_id"] for d in klass(probe)["safety"]["suppressed_domains"]] \
            == ["SECRET-C"]

    def test_a_site_inside_the_reach_still_focuses(self):
        """Deletion equivalence again, with the focus applied to both: the
        focus narrows the site facts and -- as it always has -- leaves the
        learned signals of the reader's other sites in place."""
        held = ids(("A", "B"))
        focus = compose(ALL, visible_site_ids=held, site_id=SITES["B"].id)
        assert focus == compose(("A", "B"), site_id=SITES["B"].id)
        assert klass(focus)["safety"]["error_budget"]["total"] == 900
        assert [s["id"] for s in focus["scope"]["sites"]] == [SITES["B"].id]


# ---------------------------------------------------------------------------
# 5. A persisted verdict, as a scoped reader may see it
# ---------------------------------------------------------------------------

STORED = [
    {"code": "level_below_grant", "detail": "t", "scope": "tenant"},
    {"code": "error_budget_dropped_back", "detail": "d", "scope": "site",
     "site_id": "s3-site-c"},
    {"code": "domain_suppressed", "detail": "fault-A", "scope": "domain",
     "site_id": "s3-site-a", "domain_id": "fault-A"},
    {"code": "domain_suppressed", "detail": "SECRET-C", "scope": "domain",
     "site_id": "s3-site-c", "domain_id": "SECRET-C"},
    {"code": "site_suppressed", "detail": "s", "scope": "site",
     "site_id": "s3-site-a"},
]


class TestAStoredVerdictIsNarrowedWhereItIsRead:
    def test_a_tenant_wide_reader_reads_what_was_recorded(self):
        assert visible_blocking_conditions(STORED, None) == STORED

    def test_a_row_names_a_site_only_to_a_reader_who_holds_it(self):
        kept = visible_blocking_conditions(STORED, {"s3-site-a"})
        assert [r["code"] for r in kept] == [
            "level_below_grant", "domain_suppressed", "site_suppressed",
        ]
        assert E.leaks(kept, holds=("A",)) == []

    def test_a_reader_holding_no_site_keeps_only_tenant_rows(self):
        kept = visible_blocking_conditions(STORED, frozenset())
        assert [r["code"] for r in kept] == ["level_below_grant"]

    @pytest.mark.parametrize("row", [
        {"code": "domain_suppressed", "scope": "domain", "domain_id": "SECRET-C"},
        {"code": "domain_suppressed", "scope": "domain", "site_id": "",
         "domain_id": "SECRET-C"},
        {"code": "future_condition", "detail": "SECRET-C"},
        {"code": "site_scoped_without_a_site", "scope": "site"},
        "SECRET-C",
        None,
    ], ids=["no-site-id", "blank-site-id", "no-scope", "site-scope-no-site",
            "not-a-dict", "none"])
    def test_a_row_that_cannot_name_its_site_fails_closed(self, row):
        """Only a TENANT-scoped row may pass without naming a site."""
        assert visible_blocking_conditions([row], {"s3-site-a"}) == []
        assert visible_blocking_conditions([row], None) == [row]   # control

    def test_a_site_scoped_signal_follows_its_site_and_a_cohort_signal_stays(self):
        signals = [
            {"scope_type": "cohort", "scope_ref": "Dell/R750", "statement": "c"},
            {"scope_type": "site", "scope_ref": "s3-site-a", "statement": "a"},
            {"scope_type": "site", "scope_ref": "s3-site-c",
             "statement": "SECRET-SIGNAL-C"},
        ]
        assert visible_learned_signals(signals, None) == signals
        assert [s["statement"] for s in visible_learned_signals(signals, {"s3-site-a"})] \
            == ["c", "a"]
        assert [s["statement"] for s in visible_learned_signals(signals, frozenset())] \
            == ["c"]

    def test_evidence_keeps_everything_but_the_hidden_signals(self):
        evidence = {
            "observed": "x", "outcome_evidence": {"executions": 3},
            "learned_signals": [
                {"scope_type": "site", "scope_ref": "s3-site-c",
                 "statement": "SECRET-SIGNAL-C"},
                {"scope_type": "cohort", "scope_ref": "Dell/R750", "statement": "c"},
            ],
        }
        seen = visible_verdict_evidence(evidence, {"s3-site-a"})
        assert [s["statement"] for s in seen["learned_signals"]] == ["c"]
        assert seen["observed"] == "x" and seen["outcome_evidence"] == {"executions": 3}
        assert len(evidence["learned_signals"]) == 2, "the stored record is not mutated"
        assert visible_verdict_evidence(evidence, None) == evidence
        assert visible_verdict_evidence(None, {"s3-site-a"}) == {}

    def test_a_reason_that_is_a_withheld_rows_text_is_withheld_with_it(self):
        """The evaluator copies the reason from a row's `detail`. If that
        row names a hidden fault domain, so does the reason."""
        secret = "fault domain SECRET-C is suppressed (SECRET-REASON-C)"
        rows = [
            {"code": "domain_suppressed", "detail": secret, "scope": "domain",
             "site_id": "s3-site-c", "domain_id": "SECRET-C"},
            {"code": "site_suppressed", "detail": "look before anything runs here",
             "scope": "site", "site_id": "s3-site-a"},
        ]
        assert visible_disposition_reason(secret, rows, None) == secret       # control
        assert visible_disposition_reason(secret, rows, {"s3-site-c"}) == secret
        assert visible_disposition_reason(secret, rows, {"s3-site-a"}) == WITHHELD_REASON
        assert visible_disposition_reason(secret, rows, frozenset()) == WITHHELD_REASON
        assert "SECRET" not in WITHHELD_REASON and "site" not in WITHHELD_REASON

    def test_a_reason_the_reader_may_read_is_returned_as_recorded(self):
        same = "success rate fell below the error budget"
        rows = [
            {"code": "error_budget_dropped_back", "detail": same, "scope": "site",
             "site_id": "s3-site-a"},
            {"code": "error_budget_dropped_back", "detail": same, "scope": "site",
             "site_id": "s3-site-c"},
            {"code": "level_below_grant", "detail": "tenant level 1", "scope": "tenant"},
        ]
        # The same condition at a site the reader HOLDS: their own fact.
        assert visible_disposition_reason(same, rows, {"s3-site-a"}) == same
        # A tenant-scoped reason is everyone's.
        assert visible_disposition_reason("tenant level 1", rows, frozenset()) \
            == "tenant level 1"
        # A reason that is no row's text (the agent's own ceiling, say).
        assert visible_disposition_reason("agent ceiling", rows, frozenset()) \
            == "agent ceiling"
        assert visible_disposition_reason("", rows, frozenset()) == ""
        assert visible_disposition_reason(None, None, frozenset()) == ""


# ---------------------------------------------------------------------------
# 6. The authority source is the canonical one, and only that
# ---------------------------------------------------------------------------


class TestTheAuthoritySource:
    def test_authorized_sites_refuses_a_bare_scope_and_a_missing_reader(self):
        scope = empty_scope(TENANT)
        with pytest.raises(TypeError, match="A30.24"):
            authorized_sites(scope)
        with pytest.raises(TypeError, match="A30.26"):
            authorized_sites(None)
        assert authorized_sites(read_reach(scope, "fleet.view")) == frozenset()

    @pytest.mark.parametrize("bad", [None, frozenset(), {"s3-site-a"}, "fleet.view",
                                     empty_scope(TENANT)],
                             ids=["none", "empty-set", "set", "str", "bare-scope"])
    def test_a_projection_refuses_anything_but_a_view(self, bad):
        """`None` must never come to mean "unrestricted"."""
        with pytest.raises(TypeError, match="A30.26"):
            require_autonomy_view(bad)

    def test_the_view_is_frozen_and_built_on_fleet_view(self):
        assert AUTONOMY_FACT_PERMISSION == "fleet.view"
        view = autonomy_view(empty_scope(TENANT))
        assert view == AutonomyView(sites=frozenset())
        with pytest.raises(Exception):
            view.sites = None

    async def test_the_loader_has_no_default_reader(self):
        """A caller that forgets `reach` does not get the tenant."""
        assert inspect.signature(load_autonomy_contract).parameters["reach"].default \
            is inspect.Parameter.empty
        stack = await E.build(("A",))
        async with stack.sessionmaker() as session:
            with pytest.raises(TypeError):
                await load_autonomy_contract(
                    session, tenant_id=TENANT, actor_id="u",
                    actor_species="human", permissions=[],
                )
            with pytest.raises(TypeError, match="A30.24"):
                await load_autonomy_contract(
                    session, tenant_id=TENANT, actor_id="u",
                    actor_species="human", permissions=[],
                    reach=empty_scope(TENANT),
                )


# ---------------------------------------------------------------------------
# 7. The production stack: every persona against /api/autonomy/
# ---------------------------------------------------------------------------
#
# The production app, the production `get_scope`, persisted grants, STRICT.
# Nothing is overridden but the token.


def _holds(name: str) -> tuple:
    reach = E.PERSONAS[name][1]
    return ALL if reach is None else reach


async def _read(stack, subject, path="/api/autonomy/", **params) -> dict:
    res = await stack.as_person(subject, "site_admin").get(path, **params)
    assert res.status_code == 200, res.text
    return res.json()


class TestEveryPersonaReadsItsOwnTenant:
    @pytest.mark.parametrize("name", list(E.PERSONAS))
    async def test_deletion_equivalence_over_http(self, name):
        """The persona's contract over the FULL estate equals the tenant
        owner's contract over a separately seeded estate that contains
        only the sites the persona holds."""
        full = await E.build(ALL)
        subject, _ = await E.persona(full, name)
        mine = await _read(full, subject)

        without_hidden = await E.build(_holds(name))
        oracle = (await without_hidden.as_person().get("/api/autonomy/")).json()

        assert E.normalised(mine) == E.normalised(oracle)
        assert E.leaks(mine, _holds(name)) == []

    async def test_the_tenant_reader_is_the_control(self):
        """It DOES read site C -- every fact the narrowed personas must
        not. Without this, an estate the loader could not see would pass
        every test above."""
        full = await E.build(ALL)
        subject, _ = await E.persona(full, "tenant")
        contract = await _read(full, subject)
        row = klass(contract)
        assert row["disposition"] == REQUIRES_APPROVAL
        assert row["safety"]["error_budget"] == E.expected_error_budget(ALL)
        assert row["safety"]["error_budget"]["total"] == 90_909
        assert row["safety"]["site_budget_remaining"] == E.expected_remaining(ALL)
        assert "SECRET-C" in E.expected_domains(ALL)
        assert [d["domain_id"] for d in row["safety"]["suppressed_domains"]] \
            == E.expected_domains(ALL)
        assert row["evidence"]["executions"] == 31
        assert [s["statement"] for s in row["learning"]] == [
            "cohort-knowledge", "signal-A", "signal-B", "SECRET-SIGNAL-C",
        ]

    async def test_the_org_reader_gets_a_and_b_and_nothing_from_c(self):
        full = await E.build(ALL)
        subject, _ = await E.persona(full, "org_ab")
        contract = await _read(full, subject)
        row = klass(contract)
        assert row["disposition"] == AUTONOMOUS
        assert row["safety"]["error_budget"]["total"] == 909
        assert row["safety"]["error_budget"]["sites_dropped_back"] == []
        assert row["safety"]["site_budget_remaining"] == {
            SITES["A"].id: 5, SITES["B"].id: 500,
        }
        assert row["evidence"]["executions"] == 18
        assert row["evidence"]["sites_observed"] == 2
        assert [s["statement"] for s in row["learning"]] == [
            "cohort-knowledge", "signal-A", "signal-B",
        ]
        assert klass(contract, "BMC_RESET")["disposition"] == AUTONOMOUS

    @pytest.mark.parametrize("name,total,remaining", [
        ("site_a", 9, 5), ("site_b", 900, 500),
    ])
    async def test_a_site_reader_gets_that_site_only(self, name, total, remaining):
        full = await E.build(ALL)
        subject, holds = await E.persona(full, name)
        contract = await _read(full, subject)
        row = klass(contract)
        assert row["safety"]["error_budget"]["total"] == total
        assert row["safety"]["site_budget_remaining"] == {SITES[holds[0]].id: remaining}
        assert [s["id"] for s in contract["scope"]["sites"]] == [SITES[holds[0]].id]

    @pytest.mark.parametrize("name", ["device_a", "class_server", "no_scope",
                                      "tenant_narrow"])
    async def test_a_reader_holding_no_site_gets_no_site_derived_fact(self, name):
        """R4. Device- and class-scoped principals, a principal with no
        grant, and a tenant grant narrowed away from `fleet.view`: tenant
        posture, and not one fact that came from a site."""
        full = await E.build(ALL)
        subject, _ = await E.persona(full, name)
        contract = await _read(full, subject)
        assert E.leaks(contract, holds=()) == []
        state = contract["safety_state"]
        assert state == {
            "reported": False, "sites_reporting": [], "sites_not_reporting": [],
            "suppressions": [], "error_budgets": [], "site_stop_switches": [],
        }
        assert contract["scope"]["sites"] == []
        assert contract["posture"]["stop_switch"]["sites_reporting_active"] == 0
        for row in contract["action_classes"]:
            assert row["safety"] == {
                "reported": False, "error_budget": None,
                "suppressed_domains": [], "site_budget_remaining": {},
            }
            assert row["evidence"]["executions"] == 0
            assert all(b["scope"] == "tenant" for b in row["blocking_conditions"])
            assert all(s["scope_type"] != "site" for s in row["learning"])
        # ...and the tenant-owned posture is all still there.
        assert contract["posture"]["configured_level"] == 2
        assert len(contract["posture"]["ladder"]) == 4
        assert klass(contract)["granted_at_level"] == 2

    @pytest.mark.parametrize("how", ["expired", "revoked"])
    async def test_a_lapsed_grant_on_the_hidden_site_confers_nothing(self, how):
        full = await E.build(ALL)
        subject = f"kc-s3-lapsed-{how}"
        await full.grant(subject, "site", SITES["A"].id)
        grant_c = await full.grant(subject, "site", SITES["C"].id)
        before = await _read(full, subject)
        assert klass(before)["safety"]["error_budget"]["total"] == 90_009   # control
        await full.lapse(grant_c, how=how)
        after = await _read(full, subject)
        assert E.leaks(after, holds=("A",)) == []
        assert klass(after)["safety"]["error_budget"]["total"] == 9

    async def test_an_inert_grant_on_a_vanished_site_confers_nothing(self):
        full = await E.build(ALL)
        subject = "kc-s3-inert"
        await full.grant(subject, "site", SITES["A"].id)
        await full.grant(subject, "site", "s3-site-gone")
        contract = await _read(full, subject)
        assert E.leaks(contract, holds=("A",)) == []
        assert [s["id"] for s in contract["scope"]["sites"]] == [SITES["A"].id]


class TestTheProbeOverHttp:
    @pytest.mark.parametrize("name", ["org_ab", "site_a", "device_a", "no_scope",
                                      "a_plus_narrow_c", "a_plus_approve_c",
                                      "a_plus_device_c"])
    async def test_asking_for_a_hidden_site_answers_as_if_it_did_not_exist(self, name):
        full = await E.build(ALL)
        subject, _ = await E.persona(full, name)
        absent = await _read(full, subject, site_id="s3-no-such-site")
        for key in (k for k in ALL if k not in _holds(name)):
            probe = await _read(full, subject, site_id=SITES[key].id)
            assert probe["scope"]["site_id"] == SITES[key].id      # their own echo
            probe["scope"]["site_id"] = absent["scope"]["site_id"]
            assert E.normalised(probe) == E.normalised(absent), key
            assert E.leaks(probe, _holds(name)) == [], key

    async def test_the_probe_answers_a_reader_who_holds_the_site(self):
        """CONTROL: the same request, from a reader whose reach covers C."""
        full = await E.build(ALL)
        subject, _ = await E.persona(full, "tenant")
        probe = await _read(full, subject, site_id=SITES["C"].id)
        assert klass(probe)["safety"]["error_budget"]["total"] == 90_000
        assert klass(probe)["safety"]["site_budget_remaining"] == {SITES["C"].id: 50_000}

    async def test_a_held_site_focuses_exactly_as_it_does_for_the_tenant(self):
        full = await E.build(ALL)
        subject, _ = await E.persona(full, "org_ab")
        mine = await _read(full, subject, site_id=SITES["B"].id)
        without_hidden = await E.build(("A", "B"))
        oracle = (await without_hidden.as_person().get(
            "/api/autonomy/", site_id=SITES["B"].id)).json()
        assert E.normalised(mine) == E.normalised(oracle)


# ---------------------------------------------------------------------------
# 8. The Operational Agent view, and a stored verdict on every projection
# ---------------------------------------------------------------------------


class _FakeSM:
    """Accepts a dispatch; never touches a network."""

    def __init__(self, *_a, **_kw):
        pass

    async def dispatch_action(self, endpoint, token, **kw):
        return {"accepted": True, "directive_id": "dir-s3", "reason": ""}


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(agent_runtime, "SMClient", _FakeSM)
    monkeypatch.setattr(approvals_api, "SMClient", _FakeSM)


async def _agent(stack, keys, *, name, ingress=False) -> str:
    """An ACTIVE agent bound to SEL_CLEAR over these sites.

    Created by the tenant owner through the production route, so its scope
    rows are real `cc_scope_grants` rows a machine token resolves through.
    Switched on directly: the activation ceremony is A19's, not this
    slice's.
    """
    from harkeniq_cc.db.models import CCOperationalAgent

    caps = [{"kind": "action_class", "capability_ref": "SEL_CLEAR"}]
    if ingress:
        caps.append({"kind": "ingress", "capability_ref": "proposals"})
    async with stack.as_person().client() as client:
        res = await client.post("/api/operational-agents/", json={
            "name": name,
            "scopes": [{"scope_type": "site", "scope_ref": stack.site(k)} for k in keys],
            "capabilities": caps,
        })
    assert res.status_code == 201, res.text
    agent_id = res.json()["id"]
    async with stack.sessionmaker() as session:
        agent = await session.get(CCOperationalAgent, agent_id)
        agent.status = "active"
        agent.activated_version = agent.version
        await session.commit()
    return agent_id


async def _stored(stack, agent_id):
    from sqlalchemy import select

    from harkeniq_cc.db.models import CCAgentProposal

    async with stack.sessionmaker() as session:
        return (await session.execute(
            select(CCAgentProposal).where(CCAgentProposal.agent_id == agent_id)
        )).scalars().all()


async def _evaluated(keys=("A",), incidents=("A",)):
    """The full estate, one agent, and what the REAL evaluator proposed."""
    stack = await E.build(ALL)
    agent_id = await _agent(stack, keys, name="S3 Runtime")
    for key in incidents:
        await E.seed_incident(stack, key)
    stats = await agent_runtime.run_once(stack.state, stack.tenant)
    assert stats["proposed"] == len(incidents), stats
    return stack, agent_id


def _sites_named(rows) -> set:
    return {r.get("site_id") for r in rows if r.get("site_id")}


def _statements(evidence) -> list:
    return sorted(s["statement"] for s in evidence.get("learned_signals", []))


class TestWhatTheEvaluatorRecorded:
    async def test_the_write_path_is_unchanged_and_names_every_site(self):
        """CONTROL for everything below, and the record of WHY a projection
        must narrow: the evaluator decides over the whole tenant (A30.26:
        an internal decision), so a proposal for a device at site A stores
        site C's drop-back, site C's suppressed fault domain and site C's
        learned signal."""
        stack, agent_id = await _evaluated()
        (proposal,) = await _stored(stack, agent_id)
        assert proposal.site_id == SITES["A"].id
        assert _sites_named(proposal.blocking_conditions) == {
            SITES["A"].id, SITES["B"].id, SITES["C"].id,
        }
        assert "SECRET-C" in str(proposal.blocking_conditions)
        assert "SECRET-SIGNAL-C" in _statements(proposal.evidence)


class TestAHumanReadsAProposal:
    async def _readers(self, stack):
        site_a, _ = await E.persona(stack, "site_a")
        org_ab, _ = await E.persona(stack, "org_ab")
        return site_a, org_ab

    async def test_the_agent_view_is_composed_over_the_readers_sites(self):
        stack, agent_id = await _evaluated()
        site_a, _ = await self._readers(stack)
        path = f"/api/operational-agents/{agent_id}"

        owner = (await stack.as_person().get(path)).json()
        mine = await _read(stack, site_a, path)

        def sel(view):
            return next(c for c in view["capabilities"]["action_classes"]
                        if c["action_type"] == "SEL_CLEAR")

        # CONTROL: the tenant owner is told site C withdrew the class.
        assert sel(owner)["tenant_disposition"] == REQUIRES_APPROVAL
        assert SITES["C"].id in _sites_named(sel(owner)["blocking_conditions"])
        assert sel(owner)["evidence"]["executions"] == 31
        # The site-A reader is told about site A.
        assert sel(mine)["tenant_disposition"] == AUTONOMOUS
        assert _sites_named(sel(mine)["blocking_conditions"]) <= {SITES["A"].id}
        assert sel(mine)["evidence"]["executions"] == 7
        assert sel(mine)["advancement"]["gate"] == "granted"
        assert [s["statement"] for s in sel(mine)["learning"]] == [
            "cohort-knowledge", "signal-A",
        ]
        assert mine["posture"]["sites_not_reporting"] == []
        assert owner["posture"]["sites_not_reporting"] == [SITES["D"].id]
        assert E.leaks(mine, holds=("A",)) == []

    async def test_a_reader_of_the_silent_site_is_not_told_safety_is_reported(self):
        stack, agent_id = await _evaluated(keys=("A", "D"))
        site_d, _ = await E.persona(stack, "site_d")
        mine = await _read(stack, site_d, f"/api/operational-agents/{agent_id}")
        assert mine["posture"]["safety_reported"] is False
        assert mine["posture"]["sites_not_reporting"] == [SITES["D"].id]

    @pytest.mark.parametrize("reader,names", [
        ("site_a", {"A"}), ("org_ab", {"A", "B"}), ("tenant", {"A", "B", "C"}),
    ])
    async def test_the_detail_the_list_and_the_queue_agree(self, reader, names):
        """Three projections of one stored proposal, one rule."""
        stack, agent_id = await _evaluated()
        subject, _ = await E.persona(stack, reader)
        expected_sites = {SITES[k].id for k in names}
        expected_signals = sorted(
            ["cohort-knowledge"] + [t for _i, _k, key, t, _c in E.SIGNALS if key in names]
        )

        detail = await _read(stack, subject, f"/api/operational-agents/{agent_id}")
        listed = await _read(stack, subject,
                             f"/api/operational-agents/{agent_id}/proposals")
        queue = await _read(stack, subject, "/api/approvals/")
        projections = {
            "detail": detail["proposals"][0],
            "list": listed["proposals"][0],
            "queue": next(i for i in queue["actions"]
                          if i.get("origin") == "agent")["proposal"],
        }
        (recorded,) = await _stored(stack, agent_id)
        for where, proposal in projections.items():
            assert _sites_named(proposal["blocking_conditions"]) == expected_sites, where
            assert _statements(proposal["evidence"]) == expected_signals, where
            # The recorded reason is site C's drop-back row, verbatim.
            assert proposal["disposition_reason"] == (
                recorded.disposition_reason if "C" in names else WITHHELD_REASON
            ), where
            if "C" not in names:
                assert "SECRET" not in str(proposal["blocking_conditions"]), where
                assert "SECRET" not in str(proposal["evidence"]), where
        # Tenant-scoped rows are everyone's; none was dropped.
        (stored,) = await _stored(stack, agent_id)
        tenant_rows = [r for r in stored.blocking_conditions if r["scope"] == "tenant"]
        for proposal in projections.values():
            assert [r for r in proposal["blocking_conditions"]
                    if r["scope"] == "tenant"] == tenant_rows

    async def test_the_decision_response_is_narrowed_too(self):
        stack, agent_id = await _evaluated()
        site_a, _ = await E.persona(stack, "site_a")
        (stored,) = await _stored(stack, agent_id)
        async with stack.as_person(site_a, "site_admin").client() as client:
            res = await client.post(f"/api/approvals/{stored.id}/approve")
        assert res.status_code == 200, res.text
        proposal = res.json()["proposal"]
        assert _sites_named(proposal["blocking_conditions"]) == {SITES["A"].id}
        assert "SECRET" not in str(res.json())

    async def test_approve_authority_at_a_site_is_not_fleet_view_at_that_site(self):
        """The adversarial case the docs amendment is about. This person
        may APPROVE at site C -- so the proposal at C is theirs to see --
        and holds `fleet.view` only at A. The queue is guarded by
        `action.approve`; a site's safety rows are `fleet.view` facts."""
        stack, agent_id = await _evaluated(keys=("A", "C"), incidents=("A", "C"))
        subject, _ = await E.persona(stack, "a_plus_approve_c")
        queue = await _read(stack, subject, "/api/approvals/")
        items = {i["site_id"]: i["proposal"] for i in queue["actions"]
                 if i.get("origin") == "agent"}
        assert set(items) == {SITES["A"].id, SITES["C"].id}, "both are theirs to decide"
        for proposal in items.values():
            assert _sites_named(proposal["blocking_conditions"]) == {SITES["A"].id}
            assert "SECRET" not in str(proposal["blocking_conditions"])
            assert "SECRET-SIGNAL-C" not in _statements(proposal["evidence"])

        # CONTROL: the same two grants with `fleet.view` left in at C.
        control = "kc-s3-approve-and-view-c"
        await stack.grant(control, "site", SITES["A"].id)
        await stack.grant(control, "site", SITES["C"].id)
        queue = await _read(stack, control, "/api/approvals/")
        at_c = next(i["proposal"] for i in queue["actions"]
                    if i.get("origin") == "agent" and i["site_id"] == SITES["C"].id)
        assert SITES["C"].id in _sites_named(at_c["blocking_conditions"])
        assert "SECRET-C" in str(at_c["blocking_conditions"])


class TestAMachineReadsItsOwnWork:
    """An external runtime whose grant names ONE site. Every response it
    can obtain about its own proposal, through the production scope."""

    async def _ready(self, keys=("A",)):
        stack = await E.build(ALL)
        agent_id = await _agent(stack, keys, name="S3 External", ingress=True)
        await E.seed_incident(stack, "A")
        return stack, agent_id

    async def _submit(self, stack, agent_id) -> tuple[dict, dict]:
        async with stack.as_machine(agent_id).client() as client:
            preview = await client.get(f"/api/operational-agents/{agent_id}/dry-run")
            assert preview.status_code == 200, preview.text
            (candidate,) = preview.json()["would_propose"]
            submitted = await client.post(
                f"/api/operational-agents/{agent_id}/proposals",
                json={"candidate_ref": candidate["candidate_ref"],
                      "idempotency_key": "s3-idem-0001-aaaa"},
            )
            assert submitted.status_code == 201, submitted.text
        return preview.json(), submitted.json()

    async def test_no_machine_response_names_a_site_outside_its_grant(self):
        stack, agent_id = await self._ready()
        preview, submitted = await self._submit(stack, agent_id)
        base = f"/api/operational-agents/{agent_id}"
        async with stack.as_machine(agent_id).client() as client:
            responses = {
                "dry-run": preview,
                "submit": submitted,
                "detail": (await client.get(base)).json(),
                "list": (await client.get(f"{base}/proposals")).json(),
                "receipt/submission": (await client.get(
                    f"{base}/submissions/{submitted['submission_id']}")).json(),
                "receipt/proposal": (await client.get(
                    f"{base}/proposals/{submitted['proposal_id']}")).json(),
            }
        for where, payload in responses.items():
            assert E.leaks(payload, holds=("A",)) == [], where

        # ...and each of them DID carry the rows it may carry (non-vacuous).
        rows = {
            "dry-run": preview["would_propose"][0]["blocking_conditions"],
            "submit": submitted["proposal"]["blocking_conditions"],
            "detail": responses["detail"]["proposals"][0]["proposal"]["blocking_conditions"],
            "list": responses["list"]["proposals"][0]["proposal"]["blocking_conditions"],
            "receipt/submission": responses["receipt/submission"]["proposal"]["blocking_conditions"],
            "receipt/proposal": responses["receipt/proposal"]["proposal"]["blocking_conditions"],
        }
        for where, blocking in rows.items():
            assert _sites_named(blocking) == {SITES["A"].id}, where
        assert _statements(preview["would_propose"][0]["evidence"]) == [
            "cohort-knowledge", "signal-A",
        ]
        reasons = {
            "dry-run": preview["would_propose"][0]["disposition_reason"],
            "submit": submitted["proposal"]["disposition_reason"],
            "detail": responses["detail"]["proposals"][0]["proposal"]["disposition_reason"],
            "list": responses["list"]["proposals"][0]["proposal"]["disposition_reason"],
            "receipt/submission": responses["receipt/submission"]["proposal"]["disposition_reason"],
            "receipt/proposal": responses["receipt/proposal"]["proposal"]["disposition_reason"],
        }
        for where, reason in reasons.items():
            assert reason == WITHHELD_REASON, where

        # CONTROL: there was something to withhold.
        (stored,) = await _stored(stack, agent_id)
        assert SITES["C"].id in _sites_named(stored.blocking_conditions)

    async def test_a_machine_granted_two_sites_reads_those_two(self):
        """CONTROL for the narrowing above, through the same projections:
        the agent's own grants decide, not a constant."""
        stack, agent_id = await self._ready(keys=("A", "C"))
        preview, submitted = await self._submit(stack, agent_id)
        for blocking in (preview["would_propose"][0]["blocking_conditions"],
                         submitted["proposal"]["blocking_conditions"]):
            assert _sites_named(blocking) == {SITES["A"].id, SITES["C"].id}
            assert "fault-B" not in str(blocking)
        # It holds site C, so site C's reason is its own to read.
        assert "error budget" in submitted["proposal"]["disposition_reason"]
        assert "error budget" in preview["would_propose"][0]["disposition_reason"]

    async def test_the_machine_view_of_its_own_dispositions_is_its_own_sites(self):
        stack, agent_id = await self._ready()
        async with stack.as_machine(agent_id).client() as client:
            view = (await client.get(f"/api/operational-agents/{agent_id}")).json()
        assert view["view"] == "machine"
        (row,) = view["capabilities"]["action_classes"]
        assert row["tenant_disposition"] == AUTONOMOUS          # not site C's drop-back
        assert _sites_named(row["blocking_conditions"]) <= {SITES["A"].id}
        assert view["posture"]["safety_reported"] is True
        assert view["posture"]["sites_not_reporting"] == 0


class TestTheAmbiguousItemsAreRecordedNotChanged:
    """A30.26 lists three things it REPORTS and does not change. Each is
    pinned here so that the slice which rules on it has a test to invert,
    the way F1 and P2 were pinned."""

    async def test_E1_a_hidden_site_can_still_route_a_proposal_to_a_human(self):
        """Execution semantics (S5's tenant-wide fold). The site-A reader's
        OWN contract says SEL_CLEAR is autonomous; the proposal at site A
        still waits for a human, because the evaluator decided over the
        whole tenant. No site is named. Closing this is per-site
        evaluation, which widens autonomy -- Vinod's ruling."""
        stack, agent_id = await _evaluated()
        site_a, _ = await E.persona(stack, "site_a")
        contract = await _read(stack, site_a)
        assert klass(contract)["disposition"] == AUTONOMOUS
        listed = await _read(stack, site_a,
                             f"/api/operational-agents/{agent_id}/proposals")
        proposal = listed["proposals"][0]
        assert proposal["disposition"] == REQUIRES_APPROVAL          # <- E1
        assert proposal["status"] == "awaiting_approval"             # <- E1
        # What is NOT left: which condition, or where. The stored reason is
        # site C's drop-back row verbatim, so it is withheld with that row.
        (stored,) = await _stored(stack, agent_id)
        assert "error budget" in stored.disposition_reason
        assert proposal["disposition_reason"] == WITHHELD_REASON
        assert E.leaks(proposal, holds=("A",)) == []
        # CONTROL: a reader who holds site C reads the reason as recorded.
        owner = (await stack.as_person().get(
            f"/api/operational-agents/{agent_id}/proposals")).json()
        assert owner["proposals"][0]["disposition_reason"] == stored.disposition_reason

    async def test_E2_a_stored_proposal_keeps_its_tenant_wide_outcome_statistic(self):
        stack, agent_id = await _evaluated()
        site_a, _ = await E.persona(stack, "site_a")
        listed = await _read(stack, site_a,
                             f"/api/operational-agents/{agent_id}/proposals")
        proposal = listed["proposals"][0]
        assert proposal["evidence"]["outcome_evidence"]["executions"] == 31   # not 7
        assert "31 executions in this tenant" in proposal["rationale"]

    async def test_E3_a_cohort_signal_is_tenant_knowledge(self):
        """A23's ratified decision, kept: it names a vendor and a model."""
        full = await E.build(ALL)
        subject, _ = await E.persona(full, "no_scope")
        contract = await _read(full, subject)
        assert [s["statement"] for s in klass(contract)["learning"]] == ["cohort-knowledge"]


# ---------------------------------------------------------------------------
# 9. The shape that keeps it fixed
# ---------------------------------------------------------------------------
#
# Behaviour above is the proof. What follows stops the two easy ways back:
# a new caller taking the tenant-wide contract by copying an internal one,
# and a new projection returning a stored verdict with no reader.

PACKAGE = pathlib.Path(harkeniq_cc.__file__).parent

#: The INTERNAL DECISION paths (A30.26): they reason over the whole tenant
#: and never return the contract. Adding to this set is a design decision,
#: which is the point of having to edit it.
INTERNAL_DECISIONS = {
    ("agent_runtime.py", "evaluate_agents"),          # the CC-resident evaluator
    ("agent_lifecycle.py", "run_preflight"),          # the activation gate's input
    ("api/campaigns.py", "submit_campaign"),          # autonomous vs per-wave approval
    ("api/operational_agents.py", "dry_run_agent"),   # reasons as the runtime does
    ("api/operational_agents.py", "submit_proposal"),  # the ingress re-derivation
}


def _loader_calls():
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) \
                        and getattr(node.func, "id", "") == "load_autonomy_contract":
                    reach = {k.arg: k.value for k in node.keywords}.get("reach")
                    yield str(path.relative_to(PACKAGE)), fn.name, reach


class TestTheTenantWideContractCannotBeTakenByAccident:
    def test_every_caller_names_its_reader(self):
        calls = list(_loader_calls())
        assert len(calls) >= 7, calls
        assert [c[:2] for c in calls if c[2] is None] == []

    def test_only_the_named_decision_paths_compose_over_the_whole_tenant(self):
        whole_tenant = {
            (module, fn) for module, fn, reach in _loader_calls()
            if isinstance(reach, ast.Constant) and reach.value is None
        }
        assert whole_tenant == INTERNAL_DECISIONS

    def test_no_function_narrows_a_composed_contract(self):
        """`narrow_to_sites` is how P2 was written: its input was the
        tenant-wide contract. A contract is narrowed by composing it over
        fewer sites."""
        from harkeniq_cc import autonomy

        assert not hasattr(autonomy, "narrow_to_sites")
        assert "visible_site_ids" in inspect.signature(build_autonomy).parameters


class TestNoProjectionOfAStoredVerdictIsReaderless:
    def _projections(self):
        from harkeniq_cc import receipts
        from harkeniq_cc.api import approvals, operational_agents

        return [
            operational_agents.proposal_dict,
            operational_agents.proposal_dict_with_provenance,
            approvals._proposal_item,
            receipts.proposal_block,
            receipts.build_receipt,
            receipts.machine_proposal_items,
            receipts.machine_agent_view,
        ]

    def test_each_one_requires_a_view(self):
        for fn in self._projections():
            param = inspect.signature(fn).parameters.get("view")
            assert param is not None, fn.__qualname__
            assert param.kind is inspect.Parameter.KEYWORD_ONLY, fn.__qualname__
            assert param.default is inspect.Parameter.empty, fn.__qualname__

    async def test_and_refuses_one_that_is_not_a_view(self):
        from harkeniq_cc.api.operational_agents import (
            _submission_result, proposal_dict,
        )
        from harkeniq_cc.receipts import proposal_block

        stack, agent_id = await _evaluated()
        (proposal,) = await _stored(stack, agent_id)
        for bad in (None, frozenset(), {SITES["A"].id}):
            with pytest.raises(TypeError, match="A30.26"):
                proposal_dict(proposal, view=bad)
            with pytest.raises(TypeError, match="A30.26"):
                proposal_block(proposal, full=True, view=bad)
        # The one default in the family exists for answers with NO
        # proposal; with one, it refuses exactly the same way.
        row = type("Row", (), dict(
            id="s", agent_id=agent_id, proposal_id=proposal.id, code="",
            reason="", created_at=None,
        ))()
        with pytest.raises(TypeError, match="A30.26"):
            _submission_result(row, replayed=False, proposal=proposal)

    def test_a_view_is_only_ever_built_from_the_canonical_scope(self):
        """Inside `api/`, nobody constructs an `AutonomyView` by hand."""
        offenders = []
        for path in sorted((PACKAGE / "api").glob("*.py")):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Call) \
                        and getattr(node.func, "id", "") == "AutonomyView":
                    offenders.append(f"{path.name}:{node.lineno}")
        assert offenders == []

    def test_context_has_no_way_in(self):
        """A30.26: a contextual site is not an input, because the reach has
        no field that could carry one -- and the two that are read are the
        permission-bearing ones."""
        from dataclasses import fields

        from harkeniq_cc.scope import ReadReach

        names = {f.name for f in fields(ReadReach)}
        assert {"tenant_wide", "site_ids"} <= names
        assert not [n for n in names if "context" in n]
        source = inspect.getsource(authorized_sites)
        assert "reach.tenant_wide" in source and "reach.site_ids" in source
        assert "device" not in source.split('"""')[2]


# ---------------------------------------------------------------------------
# 10. Two things this slice must not have moved
# ---------------------------------------------------------------------------


class TestWhatDidNotChange:
    async def test_a_never_granted_human_under_legacy_open_still_reads_the_tenant(self):
        """A23.10 is untouched: `legacy_open` synthesizes tenant reach for a
        human nobody ever administered, and S3 takes its reach from the
        same resolver -- so that reader still reads everything, and one
        whose only grant was revoked reads nothing."""
        from harkeniq_cc.scope import ENFORCEMENT_LEGACY_OPEN

        stack = await E.build(ALL)
        async with stack.sessionmaker() as session:
            await E.TenantSettingsRepo(session).set_enforcement(
                TENANT, ENFORCEMENT_LEGACY_OPEN, "migration:0021",
            )
            await session.commit()
        never = await _read(stack, "kc-s3-never-granted")
        assert klass(never)["safety"]["error_budget"]["total"] == 90_909

        revoked = "kc-s3-once-granted"
        grant = await stack.grant(revoked, "site", SITES["C"].id)
        await stack.lapse(grant, how="revoked")
        after = await _read(stack, revoked)
        assert after["scope"]["sites"] == []
        assert E.leaks(after, holds=()) == []

    async def test_a_stored_reason_naming_a_hidden_fault_domain_never_leaves(self):
        """The latent case the reason rule exists for. Today's composer
        never takes a reason from a domain row; a proposal whose stored
        reason DOES carry one is written here by hand, and read through
        the human and the machine projection."""
        from harkeniq_cc.api.operational_agents import proposal_dict
        from harkeniq_cc.db.models import CCAgentProposal
        from harkeniq_cc.receipts import proposal_block

        secret = "fault domain SECRET-C is suppressed (SECRET-REASON-C)"
        proposal = CCAgentProposal(
            id="s3-latent", tenant_id=TENANT, agent_id="agent", actor="op-agent:agent@v1",
            agent_version=1, site_id=SITES["A"].id, device_agent_id=SITES["A"].device,
            action_type="SEL_CLEAR", params={}, rationale="r", evidence={},
            disposition=REQUIRES_APPROVAL, disposition_reason=secret,
            blocking_conditions=[{
                "code": "domain_suppressed", "detail": secret, "scope": "domain",
                "site_id": SITES["C"].id, "domain_id": "SECRET-C",
            }],
            authorization_basis="human_approval", status="awaiting_approval",
            dedupe_key="s3-latent",
        )
        holds_a = AutonomyView(sites=frozenset({SITES["A"].id}))
        everything = AutonomyView(sites=None)
        for payload in (proposal_dict(proposal, view=holds_a),
                        proposal_block(proposal, full=True, view=holds_a),
                        proposal_block(proposal, full=False, view=holds_a)):
            assert "SECRET" not in str(payload), payload
            assert payload["disposition_reason"] == WITHHELD_REASON
        # CONTROL
        assert proposal_dict(proposal, view=everything)["disposition_reason"] == secret
        assert proposal_block(proposal, full=True, view=everything)["disposition_reason"] \
            == secret
