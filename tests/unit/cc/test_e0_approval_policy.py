"""E0.1: the approval rules, evaluated purely.

The judgement half of making a configured policy actually bind. These
pin the properties that must hold identically for a node action and an
Operational Agent proposal, because there is one implementation of them.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from harkeniq_cc.approval_policy import (
    MODE_AUTO_APPROVE,
    MODE_REQUIRE_APPROVAL,
    STATE_APPROVED,
    STATE_DENIED,
    STATE_PENDING,
    approval_block,
    effective_mode,
    evaluate_completion,
    is_member,
    required_approvers,
    resolve_policy,
)

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)


def _policy(pid="p1", action_type="*", device_type="*", risk_level="*",
            mode=MODE_REQUIRE_APPROVAL, approvers=1, group_id=None,
            status="active", name="policy"):
    return SimpleNamespace(
        id=pid, name=name, action_type=action_type, device_type=device_type,
        risk_level=risk_level, approval_mode=mode,
        required_approvers=approvers, group_id=group_id, status=status,
    )


def _record(approver="u1", decision="approved", scope_ok=True, reason=""):
    return SimpleNamespace(
        approver_ref=approver, approver_email=f"{approver}@example.com",
        decision=decision, scope_ok=scope_ok, reason=reason, decided_at=NOW,
    )


class TestPolicyResolution:
    def test_no_policies_means_no_policy(self):
        assert resolve_policy([], action_type="SEL_CLEAR") is None

    def test_wildcard_matches_everything(self):
        p = _policy()
        assert resolve_policy([p], action_type="SEL_CLEAR") is p

    def test_action_specific_beats_wildcard(self):
        wild, exact = _policy("w"), _policy("e", action_type="SEL_CLEAR")
        assert resolve_policy([wild, exact], action_type="SEL_CLEAR") is exact

    def test_action_outweighs_device_and_risk(self):
        """A rule written for one action class beats a broader rule that
        merely shares its risk band."""
        by_action = _policy("a", action_type="SEL_CLEAR")
        by_both = _policy("b", device_type="server", risk_level="low")
        got = resolve_policy(
            [by_both, by_action],
            action_type="SEL_CLEAR", device_type="server", risk="low",
        )
        assert got is by_action

    def test_a_non_matching_field_disqualifies(self):
        p = _policy(action_type="BMC_RESET")
        assert resolve_policy([p], action_type="SEL_CLEAR") is None

    def test_inactive_policies_are_ignored(self):
        p = _policy(action_type="SEL_CLEAR", status="archived")
        assert resolve_policy([p], action_type="SEL_CLEAR") is None

    def test_ties_break_deterministically(self):
        """Two runs over the same configuration must choose the same rule."""
        a = _policy("aaa", action_type="SEL_CLEAR")
        b = _policy("bbb", action_type="SEL_CLEAR")
        first = resolve_policy([a, b], action_type="SEL_CLEAR")
        second = resolve_policy([b, a], action_type="SEL_CLEAR")
        assert first is second is b

    def test_matching_is_case_insensitive(self):
        p = _policy(action_type="sel_clear")
        assert resolve_policy([p], action_type="SEL_CLEAR") is p


class TestAutoApproveIsRefused:
    def test_effective_mode_coerces_auto_approve(self):
        """Unattended execution is granted by the autonomy contract, never
        by an approval policy."""
        assert effective_mode(_policy(mode=MODE_AUTO_APPROVE)) == MODE_REQUIRE_APPROVAL

    def test_a_missing_mode_is_require_approval(self):
        assert effective_mode(SimpleNamespace()) == MODE_REQUIRE_APPROVAL


class TestRequiredApprovers:
    def test_no_policy_needs_one(self):
        """The behaviour every tenant has today, stated explicitly."""
        assert required_approvers(None) == 1

    def test_policy_count_wins(self):
        assert required_approvers(_policy(approvers=3)) == 3

    def test_group_raises_the_bar_when_the_policy_is_default(self):
        group = SimpleNamespace(required_count=2)
        assert required_approvers(_policy(approvers=1), group) == 2

    def test_a_group_never_lowers_a_number_an_operator_typed(self):
        group = SimpleNamespace(required_count=1)
        assert required_approvers(_policy(approvers=3), group) == 3

    def test_zero_is_floored_to_one(self):
        assert required_approvers(_policy(approvers=0)) == 1


class TestGroupMembership:
    def test_matches_on_subject(self):
        m = SimpleNamespace(principal_ref="kc-1", user_email="old@example.com")
        assert is_member([m], "kc-1", "new@example.com") is True

    def test_falls_back_to_email(self):
        m = SimpleNamespace(principal_ref="", user_email="a@example.com")
        assert is_member([m], "kc-9", "A@Example.com") is True

    def test_a_non_member_is_refused(self):
        m = SimpleNamespace(principal_ref="kc-1", user_email="a@example.com")
        assert is_member([m], "kc-2", "b@example.com") is False

    def test_empty_group_admits_nobody(self):
        assert is_member([], "kc-1", "a@example.com") is False


class TestCompletion:
    def test_one_of_one_is_approved(self):
        got = evaluate_completion([_record()], 1)
        assert got["state"] == STATE_APPROVED
        assert got["received"] == 1 and got["remaining"] == 0

    def test_one_of_two_stays_pending(self):
        got = evaluate_completion([_record()], 2)
        assert got["state"] == STATE_PENDING
        assert got["remaining"] == 1

    def test_two_of_two_is_approved(self):
        got = evaluate_completion([_record("u1"), _record("u2")], 2)
        assert got["state"] == STATE_APPROVED
        assert [a["approver"] for a in got["approvers"]] == [
            "u1@example.com", "u2@example.com",
        ]

    def test_one_denial_is_terminal_however_many_approvals(self):
        """An approver who objects cannot be outvoted by colleagues
        clicking faster."""
        records = [_record("u1"), _record("u2"), _record("u3", "denied")]
        got = evaluate_completion(records, 2)
        assert got["state"] == STATE_DENIED
        assert got["denied_by"] == "u3@example.com"

    def test_the_same_approver_twice_counts_once(self):
        got = evaluate_completion([_record("u1"), _record("u1")], 2)
        assert got["state"] == STATE_PENDING
        assert got["received"] == 1

    def test_an_out_of_scope_approval_does_not_count(self):
        got = evaluate_completion([_record("u1", scope_ok=False)], 1)
        assert got["state"] == STATE_PENDING
        assert got["received"] == 0

    def test_no_records_is_pending_not_approved(self):
        assert evaluate_completion([], 1)["state"] == STATE_PENDING


class TestApprovalBlock:
    def test_carries_the_policy_and_group_identity(self):
        group = SimpleNamespace(id="g1", name="SRE on-call", required_count=2)
        block = approval_block(
            _policy("p9", approvers=2, group_id="g1", name="Dual for power"),
            group, [_record()],
        )
        assert block["required"] == 2 and block["received"] == 1
        assert block["policy_id"] == "p9"
        assert block["policy_name"] == "Dual for power"
        assert block["group_name"] == "SRE on-call"
        assert block["mode"] == MODE_REQUIRE_APPROVAL

    def test_no_policy_still_produces_an_honest_block(self):
        block = approval_block(None, None, [])
        assert block["required"] == 1
        assert block["policy_id"] is None
        assert block["state"] == STATE_PENDING
