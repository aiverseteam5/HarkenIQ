"""A0: the Operational Agent bundle, evaluated purely.

The composer decides what an agent may see and propose. These tests pin
the properties that make it a governed actor rather than a privileged
one: scope fails closed, policy only ever tightens, a proposal never
outruns the tenant's own autonomy contract, and evidence is carried
rather than invented.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from harkeniq_cc.autonomy import build_autonomy
from harkeniq_cc.operational_agent import (
    BASIS_AUTONOMOUS,
    BASIS_HUMAN,
    PROPOSAL_APPROVED,
    PROPOSAL_AWAITING,
    PROPOSAL_BLOCKED,
    agent_view,
    attribution_key,
    effective_disposition,
    evaluate,
    parse_attribution,
    resolve_scope,
)

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)


def _agent(**kw):
    base = dict(
        id="ag1", tenant_id="t1", name="Night Shift", description="",
        status="active", version=1, autonomy_ceiling=0,
        require_approval_always=True, max_proposals_per_day=25,
        created_by="op@example.com", created_at=NOW, updated_at=NOW,
        activated_by="op@example.com", activated_at=NOW,
        last_evaluated_at=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _scope(scope_type, scope_ref):
    return SimpleNamespace(scope_type=scope_type, scope_ref=scope_ref)


def _cap(kind, ref):
    return SimpleNamespace(kind=kind, capability_ref=ref)


def _device(agent_id, site_id="s1", device_class="server", health="OK",
            observation="observed", vendor="Dell", model="R750"):
    return SimpleNamespace(
        agent_id=agent_id, agent_name=agent_id, site_id=site_id,
        device_class=device_class, health=health, observation=observation,
        vendor=vendor, model=model,
    )


def _contract(level=2, stop=False, outcomes=(), safety_rows=(), sites=None):
    return build_autonomy(
        tenant_id="t1",
        actor_id="op-agent:ag1@v1",
        actor_species="agent",
        permissions=["fleet.view"],
        budgets=[SimpleNamespace(
            device_type="*", level=level, budget_limit=10,
            budget_period="daily", actions_used=0,
        )],
        stop_switch=SimpleNamespace(active=stop, changed_by="", updated_at=NOW),
        outcomes=list(outcomes),
        safety_rows=list(safety_rows),
        sites=sites if sites is not None else [
            SimpleNamespace(id="s1", site_name="DC-1")
        ],
        now=NOW,
    )


class TestAttribution:
    def test_key_carries_the_bundle_version(self):
        assert attribution_key("ag1", 4) == "op-agent:ag1@v4"

    def test_round_trip(self):
        assert parse_attribution("op-agent:ag1@v4") == ("ag1", 4)

    def test_a_human_actor_is_not_an_agent_key(self):
        assert parse_attribution("user:op@example.com") is None


class TestScopeFailsClosed:
    def test_no_scope_rows_means_no_devices(self):
        """The failure mode this table exists to prevent."""
        assert resolve_scope([], [_device("d1"), _device("d2")]) == []

    def test_site_scope_selects_that_site_only(self):
        devices = [_device("d1", site_id="s1"), _device("d2", site_id="s2")]
        got = resolve_scope([_scope("site", "s1")], devices)
        assert [d.agent_id for d in got] == ["d1"]

    def test_device_class_scope(self):
        devices = [
            _device("srv", device_class="server"),
            _device("sw", device_class="switch"),
        ]
        got = resolve_scope([_scope("device_class", "switch")], devices)
        assert [d.agent_id for d in got] == ["sw"]

    def test_scope_types_union_without_duplicating(self):
        devices = [_device("d1", site_id="s1"), _device("d2", site_id="s2")]
        got = resolve_scope(
            [_scope("site", "s1"), _scope("device", "d1"), _scope("device", "d2")],
            devices,
        )
        assert sorted(d.agent_id for d in got) == ["d1", "d2"]


class TestPolicyOnlyEverTightens:
    def test_require_approval_always_downgrades_an_autonomous_class(self):
        contract = _contract(level=2)
        row = next(
            c for c in contract["action_classes"] if c["action_type"] == "SEL_CLEAR"
        )
        assert row["disposition"] == "autonomous"  # the tenant grants it
        verdict = effective_disposition(_agent(require_approval_always=True), row)
        assert verdict["disposition"] == "requires_approval"
        assert verdict["authorization_basis"] == BASIS_HUMAN
        assert any(
            b["code"] == "agent_requires_approval"
            for b in verdict["blocking_conditions"]
        )

    def test_agent_ceiling_below_the_grant_level_downgrades(self):
        contract = _contract(level=2)
        row = next(
            c for c in contract["action_classes"] if c["action_type"] == "SEL_CLEAR"
        )
        agent = _agent(require_approval_always=False, autonomy_ceiling=1)
        verdict = effective_disposition(agent, row)
        assert verdict["disposition"] == "requires_approval"
        assert any(
            b["code"] == "agent_ceiling_below_grant"
            for b in verdict["blocking_conditions"]
        )

    def test_an_agent_can_never_exceed_the_tenant(self):
        """A ceiling of 3 against a tenant at level 0 grants nothing."""
        contract = _contract(level=0)
        agent = _agent(require_approval_always=False, autonomy_ceiling=3)
        for row in contract["action_classes"]:
            verdict = effective_disposition(agent, row)
            assert verdict["disposition"] != "autonomous", row["action_type"]

    def test_an_unmapped_class_needs_a_human_rather_than_being_forbidden(self):
        """`not_budget_mapped` is about autonomy, not about permission.

        Reading it as "forbidden" would silently remove IDENTIFY_LED,
        COLLECT_DIAGNOSTICS and FAN_RESET from everything an agent may
        ever ask for -- most of the low-risk work worth delegating.
        """
        contract = _contract(level=3)
        agent = _agent(require_approval_always=False, autonomy_ceiling=3)
        row = next(
            c for c in contract["action_classes"]
            if c["action_type"] == "COLLECT_DIAGNOSTICS"
        )
        assert row["disposition"] == "not_budget_mapped"
        verdict = effective_disposition(agent, row)
        assert verdict["disposition"] == "requires_approval"
        assert verdict["authorization_basis"] == BASIS_HUMAN
        assert "always needs a named human" in verdict["disposition_reason"]

    def test_the_stop_switch_denies_an_unmapped_class_too(self):
        """Approval never overrides a safety gate (A10.3), so proposing
        into a stopped tenant would spend a decision on refused work."""
        contract = _contract(level=2, stop=True)
        row = next(
            c for c in contract["action_classes"]
            if c["action_type"] == "COLLECT_DIAGNOSTICS"
        )
        verdict = effective_disposition(_agent(), row, stop_switch_active=True)
        assert verdict["disposition"] == "denied"
        assert "stop switch" in verdict["disposition_reason"]

    def test_fenced_classes_stay_denied_whatever_the_agent_says(self):
        contract = _contract(level=3)
        agent = _agent(require_approval_always=False, autonomy_ceiling=3)
        for name in ("FIRMWARE_UPDATE", "INTERFACE_DISABLE"):
            row = next(
                c for c in contract["action_classes"] if c["action_type"] == name
            )
            assert effective_disposition(agent, row)["disposition"] == "denied"


class TestEvaluate:
    def _run(self, agent, caps, devices, incidents, contract, **kw):
        return evaluate(
            agent=agent,
            scopes=[_scope("site", "s1")],
            capabilities=caps,
            devices=devices,
            incidents_by_device=incidents,
            autonomy_contract=contract,
            now=NOW,
            **kw,
        )

    def test_no_bound_action_class_means_no_proposal(self):
        got = self._run(
            _agent(), [_cap("read", "attention")], [_device("d1")],
            {"d1": [{"incident_id": "i1", "subsystem": "log", "title": "SEL full"}]},
            _contract(),
        )
        assert got == []

    def test_no_observed_condition_means_no_proposal(self):
        """A healthy device is not an invitation to act."""
        got = self._run(
            _agent(), [_cap("action_class", "SEL_CLEAR")], [_device("d1")],
            {}, _contract(),
        )
        assert got == []

    def test_proposes_the_bound_class_for_the_observed_subsystem(self):
        got = self._run(
            _agent(), [_cap("action_class", "SEL_CLEAR")], [_device("d1")],
            {"d1": [{"incident_id": "i1", "subsystem": "log",
                     "title": "BMC event log saturated"}]},
            _contract(),
        )
        assert len(got) == 1
        p = got[0]
        assert p["action_type"] == "SEL_CLEAR"
        assert p["device_agent_id"] == "d1"
        assert p["actor"] == "op-agent:ag1@v1"
        assert p["evidence"]["incident_ids"] == ["i1"]
        assert "BMC event log saturated" in p["rationale"]
        # The device is named once, not twice (live-stack finding).
        assert p["rationale"].count("d1") == 1

    def test_does_not_propose_a_class_the_subsystem_has_no_remediation_for(self):
        """Binding BMC_RESET does not make a disk fault a BMC problem."""
        got = self._run(
            _agent(), [_cap("action_class", "BMC_RESET")], [_device("d1")],
            {"d1": [{"incident_id": "i1", "subsystem": "disk", "title": "disk"}]},
            _contract(),
        )
        assert got == []

    def test_unreachable_device_proposes_a_bmc_reset(self):
        got = self._run(
            _agent(), [_cap("action_class", "BMC_RESET")],
            [_device("d1", observation="unreachable")], {}, _contract(),
        )
        assert [p["action_type"] for p in got] == ["BMC_RESET"]
        assert got[0]["evidence"]["condition_kind"] == "unreachable"

    def test_autonomous_when_the_tenant_grants_and_the_agent_allows(self):
        agent = _agent(require_approval_always=False, autonomy_ceiling=2)
        got = self._run(
            agent, [_cap("action_class", "SEL_CLEAR")], [_device("d1")],
            {"d1": [{"incident_id": "i1", "subsystem": "log", "title": "SEL"}]},
            _contract(level=2),
        )
        assert got[0]["status"] == PROPOSAL_APPROVED
        assert got[0]["authorization_basis"] == BASIS_AUTONOMOUS
        assert got[0]["decided_by"].startswith("autonomy:")

    def test_awaits_a_human_by_default(self):
        got = self._run(
            _agent(), [_cap("action_class", "SEL_CLEAR")], [_device("d1")],
            {"d1": [{"incident_id": "i1", "subsystem": "log", "title": "SEL"}]},
            _contract(level=2),
        )
        assert got[0]["status"] == PROPOSAL_AWAITING
        assert got[0]["authorization_basis"] == BASIS_HUMAN

    def test_an_unmapped_class_is_proposed_for_a_human(self):
        got = self._run(
            _agent(), [_cap("action_class", "COLLECT_DIAGNOSTICS")],
            [_device("d1")],
            {"d1": [{"incident_id": "i1", "subsystem": "fan",
                     "title": "Fan1A has failed"}]},
            _contract(level=3),
        )
        assert len(got) == 1
        assert got[0]["action_type"] == "COLLECT_DIAGNOSTICS"
        assert got[0]["status"] == PROPOSAL_AWAITING
        assert got[0]["authorization_basis"] == BASIS_HUMAN

    def test_stop_switch_blocks_rather_than_hides(self):
        """A stopped tenant still records what the agent wanted to do."""
        got = self._run(
            _agent(), [_cap("action_class", "SEL_CLEAR")], [_device("d1")],
            {"d1": [{"incident_id": "i1", "subsystem": "log", "title": "SEL"}]},
            _contract(level=2, stop=True),
        )
        assert got[0]["status"] == PROPOSAL_BLOCKED
        assert got[0]["disposition"] == "denied"
        assert "stop switch" in got[0]["disposition_reason"]

    def test_error_budget_dropback_forces_a_human(self):
        safety = SimpleNamespace(
            site_id="s1", tenant_id="t1", reported=True, as_of=NOW,
            sm_stop_switch=False, suppressions=[],
            error_budgets=[{
                "action_type": "SEL_CLEAR", "total_count": 20,
                "success_count": 5, "failure_count": 15, "dropped_back": True,
            }],
            site_budgets={},
        )
        agent = _agent(require_approval_always=False, autonomy_ceiling=3)
        got = self._run(
            agent, [_cap("action_class", "SEL_CLEAR")], [_device("d1")],
            {"d1": [{"incident_id": "i1", "subsystem": "log", "title": "SEL"}]},
            _contract(level=2, safety_rows=[safety]),
        )
        assert got[0]["status"] == PROPOSAL_AWAITING
        assert any(
            b["code"] == "error_budget_dropped_back"
            for b in got[0]["blocking_conditions"]
        )

    def test_dedupe_key_stops_a_repeat_while_one_is_open(self):
        args = dict(
            agent=_agent(), caps=[_cap("action_class", "SEL_CLEAR")],
            devices=[_device("d1")],
            incidents={"d1": [{"incident_id": "i1", "subsystem": "log",
                               "title": "SEL"}]},
            contract=_contract(),
        )
        first = self._run(**args)
        assert len(first) == 1
        again = self._run(**args, open_dedupe_keys=[first[0]["dedupe_key"]])
        assert again == []

    def test_daily_cap_bounds_a_misconfigured_agent(self):
        devices = [_device(f"d{i}") for i in range(5)]
        incidents = {
            f"d{i}": [{"incident_id": f"i{i}", "subsystem": "log", "title": "SEL"}]
            for i in range(5)
        }
        got = self._run(
            _agent(max_proposals_per_day=2), [_cap("action_class", "SEL_CLEAR")],
            devices, incidents, _contract(),
        )
        assert len(got) == 2

    def test_evidence_carries_the_tenant_outcome_history(self):
        outcomes = [
            {"action_type": "SEL_CLEAR", "outcome": "SUCCESS",
             "fault_resolved": True, "site_id": "s1"}
            for _ in range(10)
        ]
        got = self._run(
            _agent(), [_cap("action_class", "SEL_CLEAR")], [_device("d1")],
            {"d1": [{"incident_id": "i1", "subsystem": "log", "title": "SEL"}]},
            _contract(outcomes=outcomes),
        )
        ev = got[0]["evidence"]["outcome_evidence"]
        assert ev["executions"] == 10
        assert ev["success_rate"] == 1.0
        assert "100%" in got[0]["rationale"]

    def test_a_device_outside_scope_is_invisible(self):
        got = evaluate(
            agent=_agent(),
            scopes=[_scope("site", "s1")],
            capabilities=[_cap("action_class", "SEL_CLEAR")],
            devices=[_device("d9", site_id="s2")],
            incidents_by_device={
                "d9": [{"incident_id": "i9", "subsystem": "log", "title": "SEL"}]
            },
            autonomy_contract=_contract(),
            now=NOW,
        )
        assert got == []

    def test_one_proposal_per_device_per_pass(self):
        """Two open incidents must not become two simultaneous actions."""
        got = self._run(
            _agent(),
            [_cap("action_class", "SEL_CLEAR"), _cap("action_class", "FAN_RESET")],
            [_device("d1")],
            {"d1": [
                {"incident_id": "i1", "subsystem": "log", "title": "SEL"},
                {"incident_id": "i2", "subsystem": "thermal", "title": "hot"},
            ]},
            _contract(),
        )
        assert len(got) == 1


class TestAgentView:
    def test_answers_what_it_can_see_and_do(self):
        view = agent_view(
            agent=_agent(),
            scopes=[_scope("site", "s1")],
            capabilities=[
                _cap("action_class", "SEL_CLEAR"),
                _cap("action_class", "FIRMWARE_UPDATE"),
                _cap("read", "attention"),
            ],
            devices=[_device("d1"), _device("d2", site_id="s2")],
            autonomy_contract=_contract(level=2),
            now=NOW,
        )
        assert view["scope"]["device_count"] == 1
        assert view["capabilities"]["needs_approval"] == ["SEL_CLEAR"]
        assert view["capabilities"]["denied"] == ["FIRMWARE_UPDATE"]
        assert view["agent"]["actor"] == "op-agent:ag1@v1"

    def test_says_plainly_when_an_agent_can_see_nothing(self):
        view = agent_view(
            agent=_agent(), scopes=[], capabilities=[],
            devices=[_device("d1")], autonomy_contract=_contract(), now=NOW,
        )
        assert view["scope"]["device_count"] == 0
        assert "can see nothing" in view["scope"]["statement"]

    def test_reports_a_class_the_executor_does_not_implement(self):
        view = agent_view(
            agent=_agent(), scopes=[_scope("site", "s1")],
            capabilities=[_cap("action_class", "MAKE_COFFEE")],
            devices=[_device("d1")], autonomy_contract=_contract(), now=NOW,
        )
        row = view["capabilities"]["action_classes"][0]
        assert row["known_to_executor"] is False
        assert row["disposition"] == "denied"
