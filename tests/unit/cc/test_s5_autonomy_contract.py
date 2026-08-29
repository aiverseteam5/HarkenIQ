"""S5: the autonomy contract — the pure composer and its invariants.

The composer decides what may run without a human. Its job is to be
boring and provable, so every test here pins a rule that a future change
must not quietly move:

  * the ladder the contract reports and the policy the enforcer receives
    are the SAME object (they used to be two copies);
  * no high-risk class is ever budget-grantable, at any level;
  * an unreported safety state reads UNKNOWN, never "safe";
  * evidence below the judging bar produces no rate at all, not a
    flattering one;
  * demotion is automatic, promotion is human-gated.

Real ORM rows are used wherever production will pass ORM rows. A
hand-rolled double once hid a 500 because it carried a field the real
model does not have (`pattern_id` vs `id`); that lesson applies here,
where several inputs are persisted models.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from harkeniq_cc.autonomy import (
    AUTONOMOUS,
    DENIED,
    LEVEL_2_ACTIONS,
    LEVEL_3_ACTIONS,
    MIN_EVIDENCE_OUTCOMES,
    NOT_BUDGET_MAPPED,
    PROMOTION_MIN_EXECUTIONS,
    REQUIRES_APPROVAL,
    action_risk_map,
    build_autonomy,
    grants_for_level,
    never_budget_grantable,
)
from harkeniq_cc.db.models import (
    CCAutonomyBudget,
    CCLearnedSignal,
    CCSafetyState,
    CCSite,
    CCStopSwitch,
)
from harkeniq_cc.policy_push import budget_row_to_policies

TENANT = "t1"


def _budget(level: int, limit: int = 10, used: int = 0, device_type: str = "*"):
    return CCAutonomyBudget(
        tenant_id=TENANT, device_type=device_type, level=level,
        budget_limit=limit, budget_period="daily", actions_used=used,
    )


def _site(site_id: str = "site-a", name: str = "DC-1"):
    return CCSite(
        id=site_id, tenant_id=TENANT, site_name=name,
        sm_endpoint="sm:50051", sm_token="tok",
    )


def _safety(site_id="site-a", *, reported=True, error_budgets=None,
            suppressions=None, site_budgets=None, stop=False):
    return CCSafetyState(
        site_id=site_id, tenant_id=TENANT, reported=reported,
        as_of=datetime.now(timezone.utc), sm_stop_switch=stop,
        suppressions=suppressions or [],
        error_budgets=error_budgets or [],
        site_budgets=site_budgets or {},
    )


def _outcomes(action_type: str, success: int, failure: int, site_id="site-a"):
    rows = [
        {"action_type": action_type, "outcome": "SUCCESS",
         "site_id": site_id, "fault_resolved": True}
        for _ in range(success)
    ]
    rows += [
        {"action_type": action_type, "outcome": "FAILURE",
         "site_id": site_id, "fault_resolved": False}
        for _ in range(failure)
    ]
    return rows


def _build(**over):
    kwargs = dict(
        tenant_id=TENANT, actor_id="user:u1", actor_species="human",
        permissions=["fleet.view"], budgets=[], stop_switch=None,
        outcomes=[], safety_rows=[], sites=[_site()],
    )
    kwargs.update(over)
    return build_autonomy(**kwargs)


def _by_type(result) -> dict:
    return {c["action_type"]: c for c in result["action_classes"]}


class TestMappingIsOneObject:
    """The contract and the CC->SM push cannot drift apart."""

    @pytest.mark.parametrize("level", [-1, 0, 1, 2, 3, 4, 99])
    def test_push_and_contract_agree(self, level):
        pushed = {
            p["action_type"]: p["risk_level"]
            for p in budget_row_to_policies(_budget(level))
        }
        assert pushed == grants_for_level(level)

    def test_levels_are_cumulative(self):
        assert set(grants_for_level(2)) == set(LEVEL_2_ACTIONS)
        assert set(grants_for_level(3)) == set(LEVEL_2_ACTIONS) | set(LEVEL_3_ACTIONS)

    def test_below_two_grants_nothing(self):
        assert grants_for_level(0) == {}
        assert grants_for_level(1) == {}


class TestHighRiskIsFenced:
    """The boundary S5 must never move, stated as an invariant not a list."""

    def test_no_high_risk_action_is_ever_granted(self):
        risks = action_risk_map()
        for level in range(0, 100):
            for action_type in grants_for_level(level):
                assert risks[action_type] != "high", (
                    f"level {level} grants high-risk {action_type}"
                )

    def test_the_known_four_are_fenced(self):
        risks = action_risk_map()
        for action_type in (
            "FIRMWARE_UPDATE", "FIRMWARE_ROLLBACK",
            "INTERFACE_RESET", "INTERFACE_DISABLE",
        ):
            assert never_budget_grantable(risks[action_type])

    def test_fenced_classes_are_denied_even_at_the_top_level(self):
        result = _build(budgets=[_budget(3)])
        for action_type, cls in _by_type(result).items():
            if cls["never_budget_grantable"]:
                assert cls["disposition"] == DENIED, action_type
                assert cls["advancement"]["next_level"] is None
                assert cls["advancement"]["gate"] == "not_available"

    def test_every_executable_action_appears(self):
        """A class missing from the contract is a class nobody governs."""
        result = _build()
        assert set(_by_type(result)) == set(action_risk_map())


class TestDisposition:
    def test_granted_and_clear_is_autonomous(self):
        result = _build(budgets=[_budget(2)], safety_rows=[_safety()])
        assert _by_type(result)["SEL_CLEAR"]["disposition"] == AUTONOMOUS

    def test_level_below_grant_requires_approval(self):
        result = _build(budgets=[_budget(2)], safety_rows=[_safety()])
        power = _by_type(result)["POWER_CYCLE"]
        assert power["disposition"] == REQUIRES_APPROVAL
        assert any(
            b["code"] == "level_below_grant" for b in power["blocking_conditions"]
        )

    def test_unconfigured_tenant_grants_nothing(self):
        result = _build()
        assert result["posture"]["level_source"] == "unconfigured"
        assert result["posture"]["configured_level"] == 0
        for cls in result["action_classes"]:
            assert cls["disposition"] != AUTONOMOUS

    def test_stop_switch_denies_every_grantable_class(self):
        flipped = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
        stop = CCStopSwitch(
            tenant_id=TENANT, active=True, changed_by="ops@example.com",
            updated_at=flipped,
        )
        result = _build(budgets=[_budget(3)], stop_switch=stop, safety_rows=[_safety()])
        for cls in _by_type(result).values():
            assert cls["disposition"] in (DENIED, NOT_BUDGET_MAPPED)
        posture = result["posture"]["stop_switch"]
        assert posture["active"] is True
        assert posture["changed_by"] == "ops@example.com"
        # Who flipped it AND when. The model stamps `updated_at` on a flip;
        # reading a non-existent `changed_at` silently returned null.
        assert posture["changed_at"] == flipped.isoformat()

    def test_unmapped_classes_are_named_not_hidden(self):
        """The 9 unmapped classes are visible and honestly labelled."""
        result = _build(budgets=[_budget(3)])
        unmapped = [
            c for c in result["action_classes"]
            if c["disposition"] == NOT_BUDGET_MAPPED
        ]
        assert {c["action_type"] for c in unmapped} >= {
            "IDENTIFY_LED", "COLLECT_DIAGNOSTICS", "FAN_RESET",
            "CLEAR_COUNTERS", "INTERFACE_ENABLE",
        }
        for cls in unmapped:
            assert cls["never_budget_grantable"] is False
            assert cls["advancement"]["gate"] == "roadmap"

    def test_error_budget_drop_back_withdraws_autonomy(self):
        safety = _safety(error_budgets=[{
            "action_type": "SEL_CLEAR", "total_count": 8, "success_count": 2,
            "failure_count": 6, "dropped_back": True,
        }])
        result = _build(budgets=[_budget(2)], safety_rows=[safety])
        sel = _by_type(result)["SEL_CLEAR"]
        assert sel["disposition"] == REQUIRES_APPROVAL
        assert any(
            b["code"] == "error_budget_dropped_back"
            for b in sel["blocking_conditions"]
        )
        # And the advancement line must not say "granted" while it is withdrawn.
        assert sel["advancement"]["gate"] == "operator_review"
        assert "operator" in sel["advancement"]["statement"]

    def test_exhausted_window_requires_approval(self):
        safety = _safety(site_budgets={"BMC_RESET": 0})
        result = _build(budgets=[_budget(2)], safety_rows=[safety])
        bmc = _by_type(result)["BMC_RESET"]
        assert bmc["disposition"] == REQUIRES_APPROVAL
        assert any(
            b["code"] == "budget_window_exhausted" for b in bmc["blocking_conditions"]
        )

    def test_suppression_is_context_not_a_class_verdict(self):
        """A suppressed fault domain fences targets, not an action class."""
        safety = _safety(suppressions=[
            {"domain_id": "rack-3", "trigger_reason": "direct_dependency"},
        ])
        result = _build(budgets=[_budget(2)], safety_rows=[safety])
        sel = _by_type(result)["SEL_CLEAR"]
        assert sel["disposition"] == AUTONOMOUS
        domain_blocks = [
            b for b in sel["blocking_conditions"] if b["code"] == "domain_suppressed"
        ]
        assert domain_blocks and domain_blocks[0]["scope"] == "domain"


class TestSafetyIsNeverAssumed:
    def test_no_reporting_site_reads_unknown(self):
        result = _build(budgets=[_budget(2)], safety_rows=[_safety(reported=False)])
        assert result["safety_state"]["reported"] is False
        assert result["safety_state"]["sites_not_reporting"] == ["site-a"]
        for cls in result["action_classes"]:
            assert cls["safety"]["reported"] is False

    def test_no_safety_rows_at_all_reads_unknown(self):
        result = _build(budgets=[_budget(2)], safety_rows=[])
        assert result["safety_state"]["reported"] is False

    def test_a_reporting_site_is_named(self):
        result = _build(budgets=[_budget(2)], safety_rows=[_safety()])
        assert result["safety_state"]["sites_reporting"] == ["site-a"]
        assert result["scope"]["sites"][0]["safety_reported"] is True


class TestEvidence:
    def test_below_the_bar_produces_no_rate(self):
        outcomes = _outcomes("SEL_CLEAR", success=1, failure=MIN_EVIDENCE_OUTCOMES - 2)
        result = _build(budgets=[_budget(2)], outcomes=outcomes)
        ev = _by_type(result)["SEL_CLEAR"]["evidence"]
        assert ev["sufficient"] is False
        assert ev["success_rate"] is None
        assert ev["resolution_rate"] is None

    def test_at_the_bar_a_rate_appears(self):
        outcomes = _outcomes("SEL_CLEAR", success=2, failure=6)
        result = _build(budgets=[_budget(2)], outcomes=outcomes)
        ev = _by_type(result)["SEL_CLEAR"]["evidence"]
        assert ev["executions"] == 8
        assert ev["success_rate"] == pytest.approx(0.25)
        assert ev["sites_observed"] == 1

    def test_evidence_is_per_class(self):
        outcomes = _outcomes("SEL_CLEAR", 5, 0) + _outcomes("BMC_RESET", 0, 5)
        by = _by_type(_build(budgets=[_budget(2)], outcomes=outcomes))
        assert by["SEL_CLEAR"]["evidence"]["success_rate"] == 1.0
        assert by["BMC_RESET"]["evidence"]["success_rate"] == 0.0


class TestAdvancement:
    def test_distance_is_stated_in_executions(self):
        outcomes = _outcomes("POWER_CYCLE", success=10, failure=0)
        result = _build(budgets=[_budget(2)], outcomes=outcomes)
        adv = _by_type(result)["POWER_CYCLE"]["advancement"]
        assert adv["next_level"] == 3
        assert "insufficient_executions" in adv["blocked_by"]
        assert str(PROMOTION_MIN_EXECUTIONS - 10) in adv["statement"]

    def test_qualified_evidence_still_needs_a_human(self):
        outcomes = _outcomes("POWER_CYCLE", success=PROMOTION_MIN_EXECUTIONS, failure=0)
        result = _build(budgets=[_budget(2)], outcomes=outcomes)
        adv = _by_type(result)["POWER_CYCLE"]["advancement"]
        assert adv["qualified_on_evidence"] is True
        assert adv["blocked_by"] == []
        assert adv["gate"] == "human_ratified"

    def test_a_bad_rate_blocks_advancement(self):
        outcomes = _outcomes("POWER_CYCLE", success=40, failure=20)
        result = _build(budgets=[_budget(2)], outcomes=outcomes)
        adv = _by_type(result)["POWER_CYCLE"]["advancement"]
        assert "success_rate_below_threshold" in adv["blocked_by"]
        assert adv["qualified_on_evidence"] is False


class TestActorAndScope:
    def test_actor_permissions_are_reported_not_assumed(self):
        result = _build(permissions=["fleet.view"])
        assert result["actor"]["may_observe"] is True
        assert result["actor"]["may_change_posture"] is False
        assert result["actor"]["may_approve"] is False

        elevated = _build(permissions=["fleet.view", "site.manage", "action.approve"])
        assert elevated["actor"]["may_change_posture"] is True
        assert elevated["actor"]["may_approve"] is True

    def test_wildcard_permission_is_honoured(self):
        result = _build(permissions=["*"])
        assert result["actor"]["may_change_posture"] is True

    def test_site_scope_narrows_and_never_widens(self):
        sites = [_site("site-a", "DC-1"), _site("site-b", "DC-2")]
        safety = [_safety("site-a"), _safety("site-b", reported=False)]
        outcomes = (
            _outcomes("SEL_CLEAR", 5, 0, site_id="site-a")
            + _outcomes("SEL_CLEAR", 0, 5, site_id="site-b")
        )
        result = _build(
            budgets=[_budget(2)], sites=sites, safety_rows=safety,
            outcomes=outcomes, site_id="site-a",
        )
        assert [s["id"] for s in result["scope"]["sites"]] == ["site-a"]
        assert _by_type(result)["SEL_CLEAR"]["evidence"]["success_rate"] == 1.0
        assert result["safety_state"]["sites_not_reporting"] == []

    def test_device_scoped_budgets_are_reported_as_unenforced(self):
        """SM enforcement has no device dimension; say so rather than imply it."""
        result = _build(budgets=[_budget(2), _budget(3, device_type="switch")])
        assert result["posture"]["configured_level"] == 2
        scoped = result["posture"]["device_scoped_budgets"]
        assert scoped == [{"device_type": "switch", "level": 3, "enforced": False}]


class TestLearningRidesTheContract:
    def test_a_signal_reaches_the_class_it_speaks_about(self):
        signal = CCLearnedSignal(
            tenant_id=TENANT, signal_key="cohort:dell/r750:SEL_CLEAR",
            scope_type="cohort", scope_ref="dell/r750", action_type="SEL_CLEAR",
            vendor="Dell", model="R750",
            statement="SEL_CLEAR fails 75% of the time on Dell PowerEdge R750.",
            confidence=0.4,
        )
        result = _build(budgets=[_budget(2)], learned_signals=[signal])
        by = _by_type(result)
        assert by["SEL_CLEAR"]["learning"][0]["statement"].startswith("SEL_CLEAR fails")
        assert by["BMC_RESET"]["learning"] == []


class TestContractShape:
    def test_every_field_a_future_agent_needs_is_present(self):
        """The A0/A1 consumer checklist, pinned as a test."""
        result = _build(budgets=[_budget(2)], safety_rows=[_safety()])
        assert result["contract_version"]
        for key in ("actor", "scope", "posture", "safety_state", "action_classes"):
            assert key in result
        assert {"identity", "species", "tenant_id"} <= set(result["actor"])
        cls = result["action_classes"][0]
        for key in (
            "action_type", "risk", "required_permission", "granted_at_level",
            "never_budget_grantable", "disposition", "disposition_reason",
            "blocking_conditions", "evidence", "learning", "safety",
            "approval", "advancement",
        ):
            assert key in cls, key
        assert cls["required_permission"]["observe"] == "fleet.view"
        assert cls["required_permission"]["change_posture"] == "site.manage"
