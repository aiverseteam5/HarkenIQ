"""A1: what an authorization basis entitles at the node.

The node funnel is the only thing that authorizes execution, and it has
to tell two callers apart:

  human_approval    a named person decided this. An authorization-shaped
                    lease verdict ("propose") gates autonomous INITIATIVE,
                    and a human already took the initiative, so the
                    carried approval satisfies it. This is the pre-A1
                    behaviour and must not change.

  autonomous_grant  nobody decided this except the tenant's autonomy
                    contract. The lease IS the authorization, so its
                    refusal is final. Without this distinction the S5
                    error-budget drop-back could not stop an agent, which
                    is the only thing it exists to do.

Hard gates (preconditions, stop switch, fully expired lease, blast
radius) refuse both, and always have.
"""

from __future__ import annotations

import time

import pytest

from harkeniq.agent import Agent
from harkeniq.autonomy.lease import AuthorizationLease
from harkeniq.models import Action, ActionType, VerdictSeverity


def _agent(tmp_path) -> Agent:
    return Agent({
        "agent": {"id": "agent-auth-test", "name": "node-1"},
        "bmc": {"host": "https://127.0.0.1:1", "username": "u",
                "password": "p", "verify_ssl": False},
        "skills": {"directory": str(tmp_path / "skills")},
        "checkpoint": {"path": str(tmp_path / "cp.db")},
        "actions": {"enabled": True, "approval_mode": "queue",
                    "allow_list": ["IDENTIFY_LED"]},
    })


def _lease(**kw) -> AuthorizationLease:
    now = time.time()
    base = dict(
        agent_id="agent-auth-test",
        action_classes=["IDENTIFY_LED"],
        risk_ceiling="low",
        budget_remaining={"IDENTIFY_LED": -1},
        lease_expiry=now + 3600,
        grace_expiry=now + 7200,
        suppression_domains=[],
        stop_switch=False,
        issued_at=now,
    )
    base.update(kw)
    return AuthorizationLease(**base)


def _action() -> Action:
    return Action(
        id="act-1", type=ActionType.IDENTIFY_LED, params={},
        sensor_id="disk:0", skill_name="op-agent:ag1@v1",
        verdict_severity=VerdictSeverity.WARNING,
    )


@pytest.fixture
def agent(tmp_path):
    a = _agent(tmp_path)
    # Preconditions need observable device state; give it a healthy one
    # so the lease is the only thing under test here.
    a._last_verdicts = []
    a._poll_failures = 0
    a._sel_events_forwarded = True
    return a


class TestAuthorizationBasis:
    def test_autonomous_grant_is_refused_when_the_lease_says_propose(
        self, agent,
    ):
        """The drop-back path: budget exhausted reads as `propose`."""
        agent.current_lease = _lease(budget_remaining={"IDENTIFY_LED": 0})
        allowed, reason = agent._authorize_execution(
            _action(), "autonomous_grant",
        )
        assert allowed is False
        assert "lease refuses autonomous" in reason

    def test_human_approval_proceeds_past_the_same_verdict(self, agent):
        agent.current_lease = _lease(budget_remaining={"IDENTIFY_LED": 0})
        allowed, reason = agent._authorize_execution(
            _action(), "human_approval",
        )
        assert allowed is True, reason

    def test_a_class_outside_the_lease_refuses_an_autonomous_grant(self, agent):
        agent.current_lease = _lease(action_classes=["BMC_RESET"])
        allowed, reason = agent._authorize_execution(
            _action(), "autonomous_grant",
        )
        assert allowed is False
        assert "deny" in reason

    def test_the_default_basis_is_human_approval(self, agent):
        """Pre-A1 callers keep their behaviour exactly."""
        agent.current_lease = _lease(budget_remaining={"IDENTIFY_LED": 0})
        assert agent._authorize_execution(_action())[0] is True

    def test_a_clean_lease_allows_an_autonomous_grant(self, agent):
        agent.current_lease = _lease()
        assert agent._authorize_execution(_action(), "autonomous_grant")[0] is True


class TestHardGatesRefuseBothBases:
    @pytest.mark.parametrize("basis", ["human_approval", "autonomous_grant"])
    def test_stop_switch(self, agent, basis):
        agent.current_lease = _lease(stop_switch=True)
        allowed, reason = agent._authorize_execution(_action(), basis)
        assert allowed is False
        assert "stop switch" in reason

    @pytest.mark.parametrize("basis", ["human_approval", "autonomous_grant"])
    def test_fully_expired_lease(self, agent, basis):
        past = time.time() - 10
        agent.current_lease = _lease(lease_expiry=past, grace_expiry=past)
        allowed, reason = agent._authorize_execution(_action(), basis)
        assert allowed is False
        assert "expired" in reason

    @pytest.mark.parametrize("basis", ["human_approval", "autonomous_grant"])
    def test_blast_radius(self, agent, basis):
        # FAN_RESET carries a real limit (2 per day) and no precondition,
        # so it isolates the blast-radius gate from everything else.
        agent.current_lease = _lease(
            action_classes=["FAN_RESET"], budget_remaining={"FAN_RESET": -1},
        )
        action = _action()
        action.type = ActionType.FAN_RESET
        for _ in range(2):
            agent.blast_radius.record(ActionType.FAN_RESET)
        allowed, reason = agent._authorize_execution(action, basis)
        assert allowed is False
        assert "blast radius" in reason


class TestTheBasisTravelsWithTheDirective:
    """The wire field has to reach the gate, or the distinction is theatre."""

    @pytest.mark.parametrize(
        "wire,expected",
        [
            ("autonomous_grant", "autonomous_grant"),
            ("human_approval", "human_approval"),
            # Legacy SM-authority work (firmware campaigns) predates the
            # field and must keep behaving exactly as it did.
            ("", "human_approval"),
        ],
    )
    async def test_directive_authorization_reaches_the_gate(
        self, agent, wire, expected,
    ):
        from harkeniq.proto import harkeniq_pb2

        seen: list[str] = []

        async def _capture(action, authorization="human_approval"):
            seen.append(authorization)
            action.outcome = None

        agent._execute_gated = _capture
        directive = harkeniq_pb2.Directive(
            directive_id="dir-1", kind="action", action_type="IDENTIFY_LED",
            params_json="{}", actor="op-agent:ag1@v1", authorization=wire,
        )
        await agent._run_directed_action(directive)
        assert seen == [expected]
