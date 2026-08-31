"""A2: the activation readiness contract, and skills as compositions.

The preflight is required to be *authoritative and testable*, not a UI
checklist — so this file tests it as a contract. Every dimension has a
verdict a caller can branch on, the roll-up has defined precedence, and
the four verdicts mean exactly one thing each:

    READY    satisfied
    BLOCKED  activation refused until it changes
    WARN     may proceed, but a named human must accept it
    UNKNOWN  the platform cannot tell — never satisfied, never failed

The ratified conditions are pinned here one by one, because each is a
decision somebody could otherwise quietly reverse.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from harkeniq_cc.agent_activation import (
    BLOCKED,
    DIMENSIONS,
    READY,
    UNKNOWN,
    WARN,
    acknowledgement_is_current,
    activation_grants_unattended,
    build_preflight,
    check_approval,
    check_autonomy,
    check_budget,
    check_capabilities,
    check_configuration_version,
    check_executor_reach,
    check_identity,
    check_safety,
    check_scope,
    check_skills,
    may_activate,
    roll_up,
    skill_install_targets,
    unattended_permitted,
    validate_skill_against_reach,
)


def _agent(**kw):
    base = dict(
        id="ag1", tenant_id="t1", name="Night Shift", status="draft", version=1,
        autonomy_ceiling=0, require_approval_always=True,
        max_proposals_per_day=25, execution_budget=0, budget_period="daily",
        paused_reason="", activation_acknowledged_by="",
        activation_acknowledged_version=0,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _class_row(action_type, disposition):
    return {"action_type": action_type, "disposition": disposition}


class TestVerdictAlgebra:
    def test_twelve_dimensions_are_declared(self):
        assert len(DIMENSIONS) == 12

    def test_all_ready_rolls_up_ready(self):
        assert roll_up([READY, READY, READY]) == READY

    def test_blocked_dominates_everything(self):
        assert roll_up([READY, WARN, UNKNOWN, BLOCKED]) == BLOCKED

    def test_warn_outranks_unknown(self):
        """A warning is actionable now; burying it under 'not sure' loses it."""
        assert roll_up([UNKNOWN, WARN]) == WARN

    def test_unknown_outranks_ready(self):
        assert roll_up([READY, UNKNOWN]) == UNKNOWN


class TestRatifiedCondition1_ImplementedButNotPermitted:
    """WARN + acknowledgement, preserving S6 semantics."""

    def test_implemented_not_permitted_warns_rather_than_blocking(self):
        row = check_executor_reach(
            ["SEL_CLEAR"],
            [{"device_agent_id": "d1", "declared": True,
              "implemented": ["SEL_CLEAR"], "effective": []}],
        )
        assert row["verdict"] == WARN
        assert "does not currently permit" in row["detail"] or "no node" in row["detail"]

    def test_the_warning_explains_the_node_will_refuse(self):
        row = check_executor_reach(
            ["SEL_CLEAR"],
            [{"device_agent_id": "d1", "declared": True,
              "implemented": ["SEL_CLEAR"], "effective": []}],
        )
        assert "refusal becomes attributed evidence" in row["detail"]

    def test_a_permitted_device_is_ready(self):
        row = check_executor_reach(
            ["SEL_CLEAR"],
            [{"device_agent_id": "d1", "declared": True,
              "implemented": ["SEL_CLEAR"], "effective": ["SEL_CLEAR"]}],
        )
        assert row["verdict"] == READY

    def test_mixed_fleet_warns_and_counts_both(self):
        row = check_executor_reach(
            ["SEL_CLEAR"],
            [
                {"device_agent_id": "ok", "declared": True,
                 "implemented": ["SEL_CLEAR"], "effective": ["SEL_CLEAR"]},
                {"device_agent_id": "policy", "declared": True,
                 "implemented": ["SEL_CLEAR"], "effective": []},
            ],
        )
        assert row["verdict"] == WARN
        assert row["permitted"] == 1
        assert row["warned"] == ["policy"]


class TestRatifiedCondition2_InsufficientScope:
    def test_no_scope_rows_blocks(self):
        row = check_scope([], [])
        assert row["verdict"] == BLOCKED
        assert "see no devices" in row["detail"]

    def test_scope_resolving_to_nothing_blocks(self):
        row = check_scope([SimpleNamespace(scope_type="site", scope_ref="s1")], [])
        assert row["verdict"] == BLOCKED
        assert "no devices" in row["detail"]

    def test_devices_in_scope_is_ready(self):
        row = check_scope(
            [SimpleNamespace(scope_type="site", scope_ref="s1")],
            [SimpleNamespace(agent_id="d1")],
        )
        assert row["verdict"] == READY


class TestRatifiedCondition3_AutonomyCeiling:
    """The ceiling limits UNATTENDED behaviour, never existence."""

    def test_a_zero_ceiling_agent_is_still_activatable(self):
        row = check_autonomy(_agent(autonomy_ceiling=0), ["SEL_CLEAR"],
                             {"SEL_CLEAR": _class_row("SEL_CLEAR", "autonomous")})
        assert row["verdict"] == READY, "a zero ceiling must not refuse existence"

    def test_a_zero_ceiling_grants_nothing_unattended(self):
        assert activation_grants_unattended(
            _agent(autonomy_ceiling=0, require_approval_always=False),
            ["SEL_CLEAR"], {"SEL_CLEAR": _class_row("SEL_CLEAR", "autonomous")},
        ) == []

    def test_require_approval_always_grants_nothing_unattended(self):
        assert activation_grants_unattended(
            _agent(autonomy_ceiling=3, require_approval_always=True),
            ["SEL_CLEAR"], {"SEL_CLEAR": _class_row("SEL_CLEAR", "autonomous")},
        ) == []

    def test_a_class_needing_a_human_is_reported_as_attended(self):
        row = check_autonomy(
            _agent(autonomy_ceiling=3, require_approval_always=False),
            ["SEL_CLEAR"],
            {"SEL_CLEAR": _class_row("SEL_CLEAR", "requires_approval")},
        )
        assert row["attended"] == ["SEL_CLEAR"]
        assert row["unattended"] == []


class TestRatifiedCondition4_ActivationApproval:
    """Derived from whether activation confers unattended power."""

    def test_unattended_grant_requires_activation_approval(self):
        agent = _agent(autonomy_ceiling=2, require_approval_always=False)
        rows = {"SEL_CLEAR": _class_row("SEL_CLEAR", "autonomous")}
        unattended = activation_grants_unattended(agent, ["SEL_CLEAR"], rows)
        assert unattended == ["SEL_CLEAR"]
        row = check_approval(agent, unattended)
        assert row["activation_approval_required"] is True
        assert row["verdict"] == WARN

    def test_a_human_only_agent_activates_without_extra_approval(self):
        """Frictionless where activation grants no new authority."""
        agent = _agent(autonomy_ceiling=0)
        row = check_approval(agent, [])
        assert row["activation_approval_required"] is False
        assert row["verdict"] == READY
        assert "every proposal still requires a human" in row["detail"]


class TestRatifiedCondition5_ExecutorAvailability:
    def test_no_declared_device_implements_it_blocks(self):
        row = check_capabilities(
            ["SEL_CLEAR"], {"SEL_CLEAR": _class_row("SEL_CLEAR", "requires_approval")},
            {"implemented": set(), "unknown": False},
        )
        assert row["verdict"] == BLOCKED

    def test_undeclared_devices_make_it_unknown_not_blocked(self):
        """Unknown is never zero — the rule that keeps a fleet
        mid-upgrade configurable."""
        row = check_capabilities(
            ["SEL_CLEAR"], {"SEL_CLEAR": _class_row("SEL_CLEAR", "requires_approval")},
            {"implemented": set(), "unknown": True},
        )
        assert row["verdict"] == UNKNOWN

    def test_no_bound_class_blocks(self):
        row = check_capabilities([], {}, {"implemented": set(), "unknown": False})
        assert row["verdict"] == BLOCKED
        assert "propose nothing" in row["detail"]

    def test_all_undeclared_reach_is_unknown(self):
        row = check_executor_reach(
            ["SEL_CLEAR"], [{"device_agent_id": "d1", "declared": False}]
        )
        assert row["verdict"] == UNKNOWN
        assert "not the same as zero" in row["detail"]


class TestRatifiedCondition6_InvalidIdentity:
    def test_unresolvable_grants_block_by_name(self):
        """The E1.4 lockout lesson: a silent lockout looks exactly like
        correct strict mode, so it is refused BY NAME."""
        row = check_identity(_agent(), realm_ok=False)
        assert row["verdict"] == BLOCKED
        assert "realm" in row["detail"]

    def test_unresolvable_state_is_unknown_not_valid(self):
        row = check_identity(_agent(), realm_ok=None)
        assert row["verdict"] == UNKNOWN

    def test_a_retired_agent_cannot_activate(self):
        row = check_identity(_agent(status="retired"), realm_ok=True)
        assert row["verdict"] == BLOCKED


class TestRatifiedCondition7_ConfigurationVersion:
    def test_a_preflight_for_an_older_version_blocks(self):
        row = check_configuration_version(_agent(version=2), preflight_version=1)
        assert row["verdict"] == BLOCKED
        assert "re-run preflight" in row["detail"]

    def test_no_preflight_warns(self):
        assert check_configuration_version(_agent(), None)["verdict"] == WARN

    def test_a_matching_preflight_is_ready(self):
        assert check_configuration_version(
            _agent(version=3), preflight_version=3
        )["verdict"] == READY

    def test_an_acknowledgement_for_an_older_version_does_not_count(self):
        agent = _agent(version=2, activation_acknowledged_by="ops@x",
                       activation_acknowledged_version=1)
        assert acknowledgement_is_current(agent) is False

    def test_a_current_acknowledgement_counts(self):
        agent = _agent(version=2, activation_acknowledged_by="ops@x",
                       activation_acknowledged_version=2)
        assert acknowledgement_is_current(agent) is True


class TestBudget:
    """D2: counts EXECUTIONS; exhaustion stops unattended only."""

    def test_no_budget_configured_is_ready(self):
        row = check_budget(_agent(execution_budget=0), executions_used=99)
        assert row["verdict"] == READY
        assert "tenant and site budgets still apply" in row["detail"]

    def test_remaining_budget_is_ready(self):
        row = check_budget(_agent(execution_budget=10), executions_used=3)
        assert row["verdict"] == READY
        assert row["remaining"] == 7

    def test_exhaustion_warns_and_says_the_agent_is_not_disabled(self):
        row = check_budget(_agent(execution_budget=10), executions_used=10)
        assert row["verdict"] == WARN
        assert row["exhausted"] is True
        assert "still observes, proposes" in row["detail"]

    def test_exhaustion_stops_unattended_only(self):
        agent = _agent(execution_budget=5, autonomy_ceiling=3,
                       require_approval_always=False)
        ok, reason = unattended_permitted(agent, executions_used=5)
        assert ok is False
        assert "may still propose" in reason
        assert "human may still approve" in reason

    def test_within_budget_permits_unattended(self):
        agent = _agent(execution_budget=5, autonomy_ceiling=3,
                       require_approval_always=False)
        assert unattended_permitted(agent, executions_used=4)[0] is True

    def test_a_paused_agent_never_runs_unattended(self):
        agent = _agent(paused_reason="held by ops", autonomy_ceiling=3,
                       require_approval_always=False)
        assert unattended_permitted(agent, 0)[0] is False


class TestSafety:
    def test_a_paused_agent_blocks(self):
        row = check_safety(_agent(paused_reason="held"), False, True)
        assert row["verdict"] == BLOCKED

    def test_a_tenant_stop_switch_blocks(self):
        row = check_safety(_agent(), stop_switch_active=True, safety_reported=True)
        assert row["verdict"] == BLOCKED

    def test_unreported_safety_is_unknown_never_clear(self):
        row = check_safety(_agent(), False, safety_reported=False)
        assert row["verdict"] == UNKNOWN
        assert "never clear" in row["detail"]


class TestSkillsAreCompositionsNotPermissions:
    def test_a_diagnosis_only_skill_is_always_usable(self):
        r = validate_skill_against_reach("s", [], set(), set(), False)
        assert r["usable"] is True

    def test_a_skill_recommending_an_unimplemented_action_is_unusable(self):
        r = validate_skill_against_reach(
            "s", ["INTERFACE_RESET"], {"SEL_CLEAR"}, {"SEL_CLEAR"}, False
        )
        assert r["usable"] is False
        assert "no executor in this platform implements" in r["reason"]

    def test_a_skill_unreachable_in_scope_is_unusable(self):
        r = validate_skill_against_reach(
            "s", ["SEL_CLEAR"], {"SEL_CLEAR"}, set(), False
        )
        assert r["usable"] is False
        assert "no device in this agent's scope" in r["reason"]

    def test_unknown_scope_makes_it_unknown_not_unusable(self):
        r = validate_skill_against_reach(
            "s", ["SEL_CLEAR"], {"SEL_CLEAR"}, set(), True
        )
        assert r["usable"] is None

    def test_an_unusable_skill_blocks_activation(self):
        row = check_skills([{"skill_id": "s1", "usable": False}])
        assert row["verdict"] == BLOCKED
        assert "no device in scope can perform" in row["detail"]

    def test_an_unknown_skill_is_unknown(self):
        assert check_skills([{"skill_id": "s1", "usable": None}])["verdict"] == UNKNOWN

    def test_no_skills_is_ready(self):
        assert check_skills([])["verdict"] == READY


class TestSkillInstallTargeting:
    def test_it_installs_only_where_the_skill_can_act(self):
        r = skill_install_targets(["SEL_CLEAR"], [
            {"device_agent_id": "can", "declared": True, "implemented": ["SEL_CLEAR"]},
            {"device_agent_id": "cannot", "declared": True,
             "implemented": ["IDENTIFY_LED"]},
        ])
        assert r["install"] == ["can"]
        assert [s["device_agent_id"] for s in r["skip"]] == ["cannot"]

    def test_a_skipped_device_says_why(self):
        r = skill_install_targets(["SEL_CLEAR"], [
            {"device_agent_id": "cannot", "declared": True,
             "implemented": ["IDENTIFY_LED"]},
        ])
        assert "does not implement SEL_CLEAR" in r["skip"][0]["reason"]

    def test_an_undeclared_device_still_receives_it(self):
        """Unknown is not incapable, and the node's allow list remains
        the final authority anyway."""
        r = skill_install_targets(["SEL_CLEAR"], [
            {"device_agent_id": "undeclared", "declared": False},
        ])
        assert r["install"] == ["undeclared"]

    def test_a_diagnosis_only_skill_installs_everywhere_in_scope(self):
        r = skill_install_targets([], [
            {"device_agent_id": "a", "declared": True, "implemented": []},
            {"device_agent_id": "b", "declared": False},
        ])
        assert r["install"] == ["a", "b"]
        assert r["skip"] == []


def _preflight(**over):
    base = dict(
        agent=_agent(), tenant_id="t1",
        scope_rows=[SimpleNamespace(scope_type="site", scope_ref="s1")],
        in_scope_devices=[SimpleNamespace(agent_id="d1")],
        bound_classes=["SEL_CLEAR"], skill_rows=[],
        class_rows={"SEL_CLEAR": _class_row("SEL_CLEAR", "requires_approval")},
        reach={"implemented": {"SEL_CLEAR"}, "unknown": False},
        per_device_reach=[{"device_agent_id": "d1", "declared": True,
                           "implemented": ["SEL_CLEAR"],
                           "effective": ["SEL_CLEAR"]}],
        executions_used=0, stop_switch_active=False, safety_reported=True,
        realm_ok=True, preflight_version=1,
    )
    base.update(over)
    return build_preflight(**base)


class TestThePreflightContract:
    def test_it_reports_every_dimension(self):
        result = _preflight()
        assert {d["dimension"] for d in result["dimensions"]} == set(DIMENSIONS)

    def test_a_clean_configuration_is_ready_and_activatable(self):
        result = _preflight()
        assert result["overall"] == READY
        assert result["can_activate"] is True
        assert result["requires_acknowledgement"] is False

    def test_it_is_machine_readable_and_human_readable(self):
        """Both, deliberately: the Console and the activation gate are
        two consumers, and if they could disagree an operator would
        approve something different from what runs."""
        result = _preflight()
        assert set(result["by_dimension"]) == set(DIMENSIONS)
        for row in result["dimensions"]:
            assert row["verdict"] in (READY, WARN, BLOCKED, UNKNOWN)
            assert row["detail"], f"{row['dimension']} has no human sentence"

    def test_it_carries_its_own_version(self):
        result = _preflight(agent=_agent(version=7), preflight_version=7)
        assert result["configuration_version"] == 7
        assert "version 7" in result["contract"]["versioning"]

    def test_it_states_that_it_confers_nothing(self):
        c = _preflight()["contract"]
        assert "grants nothing" in c["authority"]
        assert "final execution authority" in c["authority"]
        assert "never read as satisfied and never as failed" in c["unknown"]

    def test_a_blocked_dimension_blocks_the_whole_result(self):
        result = _preflight(scope_rows=[], in_scope_devices=[])
        assert result["overall"] == BLOCKED
        assert result["can_activate"] is False
        assert "scope" in result["blocked_dimensions"]

    def test_a_warning_requires_acknowledgement_but_not_refusal(self):
        result = _preflight(
            per_device_reach=[{"device_agent_id": "d1", "declared": True,
                               "implemented": ["SEL_CLEAR"], "effective": []}],
        )
        assert result["overall"] == WARN
        assert result["can_activate"] is True
        assert result["requires_acknowledgement"] is True

    def test_unattended_grant_is_surfaced_for_activation_approval(self):
        result = _preflight(
            agent=_agent(autonomy_ceiling=2, require_approval_always=False),
            class_rows={"SEL_CLEAR": _class_row("SEL_CLEAR", "autonomous")},
        )
        assert result["requires_activation_approval"] is True
        assert result["unattended_classes"] == ["SEL_CLEAR"]


class TestTheActivationGate:
    def test_activation_without_a_preflight_is_refused(self):
        ok, reason = may_activate(None, _agent())
        assert ok is False
        assert "preflight is mandatory" in reason

    def test_a_stale_preflight_is_refused(self):
        result = _preflight()
        ok, reason = may_activate(result, _agent(version=2))
        assert ok is False
        assert "re-run preflight" in reason

    def test_a_blocked_preflight_is_refused_and_names_why(self):
        result = _preflight(scope_rows=[], in_scope_devices=[])
        ok, reason = may_activate(result, _agent())
        assert ok is False
        assert "scope" in reason

    def test_warnings_require_a_named_human_first(self):
        result = _preflight(
            per_device_reach=[{"device_agent_id": "d1", "declared": True,
                               "implemented": ["SEL_CLEAR"], "effective": []}],
        )
        ok, reason = may_activate(result, _agent())
        assert ok is False
        assert "named person must accept" in reason

    def test_an_acknowledged_warning_permits_activation(self):
        agent = _agent(activation_acknowledged_by="ops@x",
                       activation_acknowledged_version=1)
        result = _preflight(
            agent=agent,
            per_device_reach=[{"device_agent_id": "d1", "declared": True,
                               "implemented": ["SEL_CLEAR"], "effective": []}],
        )
        assert may_activate(result, agent)[0] is True

    def test_an_acknowledgement_from_an_older_version_does_not_carry(self):
        """D3: an edit must not silently carry acceptance forward onto a
        configuration the person never saw."""
        agent = _agent(version=2, activation_acknowledged_by="ops@x",
                       activation_acknowledged_version=1)
        result = _preflight(agent=agent, preflight_version=2,
                            per_device_reach=[{"device_agent_id": "d1",
                                               "declared": True,
                                               "implemented": ["SEL_CLEAR"],
                                               "effective": []}])
        ok, reason = may_activate(result, agent)
        assert ok is False
        assert "named person must accept" in reason

    def test_a_clean_preflight_permits_activation(self):
        assert may_activate(_preflight(), _agent())[0] is True
