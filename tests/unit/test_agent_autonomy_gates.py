"""QA-020: the R3a autonomy gate chain, finally wired into the Agent.

Covers: gate ordering (allow-list bypass, preconditions, stop switch,
blast radius), budget/blast accounting after success, lease mirroring on
heartbeat ack, and outcome verification (UNKNOWN producible).
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from harkeniq.agent import Agent
from harkeniq.autonomy.lease import AuthorizationLease
from harkeniq.models import Action, ActionStatus, ActionType, VerdictSeverity


class FakeExecutor:
    """Records executions; succeeds unless told otherwise."""

    def __init__(self, allow_list=None):
        self.allow_list = allow_list or [
            "IDENTIFY_LED", "COLLECT_DIAGNOSTICS", "FAN_RESET",
            "SEL_CLEAR", "BMC_RESET", "POWER_CYCLE", "POWER_CAP_ADJUST",
        ]
        self.executed: list[Action] = []

    async def execute(self, action: Action):
        from harkeniq.models import ActionOutcome

        self.executed.append(action)
        if action.type.value not in self.allow_list:
            action.status = ActionStatus.FAILED
            action.outcome = ActionOutcome(
                action_id=action.id, type=action.type, target="",
                success=False,
                error_message=f"Action type {action.type.value} not in allow list",
            )
            return action.outcome
        action.status = ActionStatus.COMPLETED
        action.outcome = ActionOutcome(
            action_id=action.id, type=action.type, target="",
            success=True,
        )
        return action.outcome


def _healthy_device():
    return SimpleNamespace(
        health_rollup=SimpleNamespace(overall="OK"),
        fans=[SimpleNamespace(health="OK")],
    )


def _lease(**overrides):
    now = time.time()
    fields = dict(
        agent_id="agent-1",
        action_classes=["IDENTIFY_LED", "COLLECT_DIAGNOSTICS", "FAN_RESET"],
        risk_ceiling="low",
        budget_remaining={"FAN_RESET": -1},
        lease_expiry=now + 300,
        grace_expiry=now + 360,
        suppression_domains=[],
        stop_switch=False,
        issued_at=now,
    )
    fields.update(overrides)
    return AuthorizationLease(**fields)


def _action(action_type=ActionType.FAN_RESET, params=None):
    action = Action(
        id="act-1", type=action_type, params=params or {},
        status=ActionStatus.APPROVED, sensor_id="fan:1",
        skill_name="fan-health", verdict_severity=VerdictSeverity.CRITICAL,
    )
    return action


@pytest.fixture
def agent():
    a = Agent({"bmc": {"host": "https://bmc"}})
    a.executor = FakeExecutor()
    a._last_device = _healthy_device()
    return a


class TestGateChain:
    async def test_healthy_approved_action_executes(self, agent):
        action = _action()
        await agent._execute_gated(action)
        assert action.status == ActionStatus.COMPLETED
        assert len(agent.executor.executed) == 1

    async def test_stop_switch_from_lease_refuses(self, agent):
        agent.current_lease = _lease(stop_switch=True)
        action = _action()
        await agent._execute_gated(action)
        assert action.status == ActionStatus.FAILED
        assert "stop switch" in action.outcome.error_message
        assert agent.executor.executed == []

    async def test_stop_switch_from_budget_enforcer_refuses(self, agent):
        agent.budget.update_from_lease({}, stop_switch=True)
        action = _action()
        await agent._execute_gated(action)
        assert action.status == ActionStatus.FAILED
        assert "stop switch" in action.outcome.error_message
        assert agent.executor.executed == []

    async def test_precondition_refusal_sel_clear(self, agent):
        """SEL_CLEAR preconditions (forwarded + >=80% full) cannot be
        satisfied until the log loop lands (QA-024) — fail-closed."""
        action = _action(ActionType.SEL_CLEAR)
        await agent._execute_gated(action)
        assert action.status == ActionStatus.FAILED
        assert "preconditions failed" in action.outcome.error_message
        assert agent.executor.executed == []

    async def test_blast_radius_limits_fan_reset(self, agent):
        # DEFAULT_LIMITS: FAN_RESET max 2 per day + 300s cooldown
        first = _action()
        await agent._execute_gated(first)
        assert first.status == ActionStatus.COMPLETED
        second = _action()
        await agent._execute_gated(second)
        assert second.status == ActionStatus.FAILED
        assert "blast radius" in second.outcome.error_message
        assert len(agent.executor.executed) == 1

    async def test_allow_list_refusal_stays_canonical(self, agent):
        """Actions outside the allow list skip the gates so the executor
        produces its canonical R-X6 refusal."""
        agent.executor = FakeExecutor(allow_list=["IDENTIFY_LED"])
        action = _action(ActionType.SEL_CLEAR)  # preconditions would refuse
        await agent._execute_gated(action)
        assert len(agent.executor.executed) == 1
        assert "not in allow list" in action.outcome.error_message

    async def test_lease_class_deny_satisfied_by_approval(self, agent):
        """Class-membership deny gates autonomous initiative, not an
        action that carries an explicit approval."""
        agent.current_lease = _lease(action_classes=["IDENTIFY_LED"])
        agent._sm_connected = True
        action = _action()  # FAN_RESET not in lease classes
        await agent._execute_gated(action)
        assert action.status == ActionStatus.COMPLETED

    async def test_fully_expired_lease_refuses(self, agent):
        now = time.time()
        agent.current_lease = _lease(
            lease_expiry=now - 120, grace_expiry=now - 60,
        )
        action = _action()
        await agent._execute_gated(action)
        assert action.status == ActionStatus.FAILED
        assert "expired" in action.outcome.error_message


class TestAccounting:
    async def test_success_consumes_budget_and_records_blast(self, agent):
        agent.budget.update_from_lease({"FAN_RESET": 2}, stop_switch=False)
        action = _action()
        await agent._execute_gated(action)
        assert action.status == ActionStatus.COMPLETED
        state = agent.budget.get_state()
        assert state["FAN_RESET"]["remaining"] == 1
        assert agent.blast_radius.allows(ActionType.FAN_RESET) is False  # cooldown


class TestLeaseMirroring:
    async def test_heartbeat_ack_updates_budget_enforcer(self):
        """_process_heartbeat_ack mirrors the lease into the budget
        enforcer (documented in budget.py, never called until QA-020)."""
        from harkeniq.autonomy.identity import AgentIdentity
        from harkeniq.autonomy.lease import build_lease_payload
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )
        from cryptography.hazmat.primitives import serialization

        agent = Agent({"bmc": {"host": "https://bmc"}})
        identity = AgentIdentity.generate()
        sm_key = Ed25519PrivateKey.generate()
        identity.set_sm_public_key(
            sm_key.public_key().public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )
        agent.agent_identity = identity

        payload = build_lease_payload(
            agent_id=identity.agent_id,
            action_classes=["FAN_RESET"],
            risk_ceiling="low",
            budget_remaining={"FAN_RESET": 3},
            stop_switch=True,
        )
        lease_bytes = payload + sm_key.sign(payload)
        ack = SimpleNamespace(authorization_lease=lease_bytes)
        agent._process_heartbeat_ack(ack)

        assert agent.current_lease is not None
        assert agent.budget.stop_switch_active is True
        assert agent.budget.get_state()["FAN_RESET"]["remaining"] == 3


class TestVerification:
    async def test_post_state_honest_fields(self, agent):
        class FakeProtocol:
            async def poll_sensors(self):
                return _healthy_device()
        agent.protocol = FakeProtocol()
        post = await agent._collect_post_state()
        assert post["bmc_responsive"] is True
        assert post["fan_rpm_healthy"] is True
        assert post["agent_registered"] is False
        assert "sel_entry_count" not in post  # unobservable -> absent

    async def test_unreachable_bmc_reported(self, agent):
        class DeadProtocol:
            async def poll_sensors(self):
                raise TimeoutError("bmc gone")
        agent.protocol = DeadProtocol()
        post = await agent._collect_post_state()
        assert post["bmc_responsive"] is False

    async def test_verification_unknown_when_unobservable(self, agent, caplog):
        """SEL_CLEAR verification needs sel_entry_count, which the agent
        cannot observe yet -> UNKNOWN, never a fabricated FAILURE."""
        class FakeProtocol:
            async def poll_sensors(self):
                return _healthy_device()
        agent.protocol = FakeProtocol()
        action = _action(ActionType.SEL_CLEAR)
        with caplog.at_level("INFO", logger="harkeniq.agent"):
            await agent._verify_action(action, delay=0)
        assert "UNKNOWN" in caplog.text

    async def test_verification_success_for_fan_reset(self, agent, caplog):
        class FakeProtocol:
            async def poll_sensors(self):
                return _healthy_device()
        agent.protocol = FakeProtocol()
        action = _action(ActionType.FAN_RESET)
        with caplog.at_level("INFO", logger="harkeniq.agent"):
            await agent._verify_action(action, delay=0)
        assert "SUCCESS" in caplog.text

    async def test_verification_scheduled_after_success(self, agent):
        agent.config["actions"] = {"verification_window_scale": 0.0}

        class FakeProtocol:
            async def poll_sensors(self):
                return _healthy_device()
        agent.protocol = FakeProtocol()
        action = _action()
        await agent._execute_gated(action)
        assert len(agent._verification_tasks) == 1
        await list(agent._verification_tasks)[0]
