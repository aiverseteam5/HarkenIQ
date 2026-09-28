"""S3-E1 (A30.37): dispatch, D11 and the preflight -- the database half.

A real app, real repositories, the real resolver and the ONE dispatch gate,
driven down both production paths (the synchronous approval and the
background pass), with a Site Manager that records every CC -> SM call:

* E-N of the brief's matrix: a global constraint that appears after approval
  WITHHOLDS on both bases and leaves the decision and its ledger as they
  were; one that clears lets the SAME proposal dispatch exactly once; scope,
  pause and binding keep their own attribution; D11 refuses an unattended
  class nobody approved; a stale report narrows; a member that raises fails
  closed; the empty registry changes nothing; and the node still refuses on
  its own;
* no mode is ever converted by these gates (D5): a withheld autonomous grant
  stays one;
* D11's recovery -- the activation-approved set is the one a human approved,
  found from the activation-time preflight and the E0.1 ledger;
* the preflight's R5: a site the agent cannot reach reporting no longer
  turns UNKNOWN into READY, and F-6 is closed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
import sqlalchemy as sa

from harkeniq_cc import agent_runtime
from harkeniq_cc import global_safety as G
from harkeniq_cc.agent_activation import (
    SITE_LOCAL_WITHHELD_REASON,
    UNATTENDED_NOT_APPROVED_REASON,
)
from harkeniq_cc.api.deps import get_current_user
from harkeniq_cc.app import create_app
from harkeniq_cc.auth import ROLE_PERMISSIONS, UserContext, configure_auth
from harkeniq_cc.config import CCConfig
from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_cc.db.models import (
    CCAgentCapability,
    CCAgentPreflight,
    CCAgentProposal,
    CCApprovalRecord,
    CCAutonomyBudget,
    CCFleetCache,
    CCIncident,
    CCOperationalAgent,
    CCSafetyState,
    CCScopeGrant,
    CCSite,
)
from harkeniq_cc.runtime import AppState

from tests.unit.cc.conftest import seed_tenant_people
from tests.unit.cc.s3e1_support import record_activation

TENANT = "t1"
OWNER = ("kc-owner", "owner@example.com", "tenant_owner")

CAPS = {
    "reach_known": True,
    "implemented": ["SEL_CLEAR", "IDENTIFY_LED", "BMC_RESET"],
    "allowed": ["SEL_CLEAR", "IDENTIFY_LED", "BMC_RESET"],
    "effective": ["SEL_CLEAR", "IDENTIFY_LED", "BMC_RESET"],
}


# ---------------------------------------------------------------------------
# The Site Manager, observed; the gate, controlled
# ---------------------------------------------------------------------------


class FakeSM:
    calls: list = []
    raise_: bool = False
    accepted: bool = True
    reason: str = ""

    def __init__(self, *_a, **_kw):
        pass

    async def dispatch_action(self, endpoint, token, **kw):
        if FakeSM.raise_:
            raise ConnectionError("site manager unreachable")
        FakeSM.calls.append(kw)
        if not FakeSM.accepted:
            return {"accepted": False, "reason": FakeSM.reason}
        return {"accepted": True, "directive_id": f"d{len(FakeSM.calls)}", "reason": ""}


class Toggle:
    """TEST-ONLY gate member, as the live probe is: constrains while `on`."""

    member_id = "test_toggle"

    def __init__(self):
        self.on = False
        self.raises = False

    def evaluate(self, context):
        if self.raises:
            raise RuntimeError("SECRET member failure at site-HIDDEN")
        return G.MemberVerdict.CONSTRAIN if self.on else G.MemberVerdict.CLEAR


@pytest.fixture(autouse=True)
def _world(monkeypatch):
    FakeSM.calls, FakeSM.raise_, FakeSM.accepted, FakeSM.reason = [], False, True, ""
    monkeypatch.setattr("harkeniq_cc.agent_runtime.SMClient", FakeSM)
    monkeypatch.setattr("harkeniq_cc.api.approvals.SMClient", FakeSM)
    toggle = Toggle()
    monkeypatch.setattr(G, "_ACTIVE", (toggle,))
    yield toggle


# ---------------------------------------------------------------------------
# A real stack
# ---------------------------------------------------------------------------


class Stack:
    def __init__(self, app, state):
        self.app, self.state = app, state
        self.sessionmaker = state.sessionmaker
        self.sites: dict[str, str] = {}

    def client(self):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test",
        )


async def _stack() -> Stack:
    configure_auth("", "", "", insecure=True)
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    state = AppState(
        config=CCConfig(tenant_id=TENANT, insecure=True),
        engine=engine, sessionmaker=make_sessionmaker(engine),
    )
    app = create_app(state)
    stack = Stack(app, state)
    await seed_tenant_people(state.sessionmaker, TENANT, [(OWNER[0], OWNER[2])])

    async def _owner():
        return UserContext(
            user_id=OWNER[0], email=OWNER[1], tenant_id=TENANT, role=OWNER[2],
            permissions=list(ROLE_PERMISSIONS[OWNER[2]]),
        )

    app.dependency_overrides[get_current_user] = _owner
    return stack


async def _estate(stack, *, level=2, keys=("A", "B")) -> Stack:
    """Sites that REPORT, one device each, an open log incident at A."""
    now = datetime.now(timezone.utc)
    async with stack.sessionmaker() as session:
        for key in keys:
            row = CCSite(tenant_id=TENANT, site_name=f"DC-{key}",
                         sm_endpoint=f"sm-{key}:50051", sm_token=f"tok-{key}")
            session.add(row)
            await session.flush()
            stack.sites[key] = row.id
            session.add(CCFleetCache(
                site_id=row.id, agent_id=f"node-{key}", agent_name=f"node-{key}",
                vendor="Dell", model="R750", device_class="server",
                observation="observed", health="Critical", capabilities=CAPS,
            ))
            session.add(CCSafetyState(
                site_id=row.id, tenant_id=TENANT, reported=True, as_of=now,
                ingested_at=now, sm_stop_switch=False, suppressions=[],
                error_budgets=[], site_budgets={},
            ))
        session.add(CCAutonomyBudget(
            tenant_id=TENANT, device_type="*", level=level,
            budget_limit=100, budget_period="daily",
        ))
        session.add(CCIncident(
            incident_id="inc-A", tenant_id=TENANT, site_id=stack.sites["A"],
            kind="device", status="open", title="BMC event log saturated on node-A",
            device_agent_id="node-A", subsystem="log", confidence=0.9,
        ))
        await session.commit()
    return stack


async def _safety(stack, key, **fields):
    async with stack.sessionmaker() as session:
        row = await session.get(CCSafetyState, stack.sites[key])
        for name, value in fields.items():
            setattr(row, name, value)
        await session.commit()


async def _agent(stack, *, autonomous=True, approve_activation=True, keys=("A",)) -> str:
    """An agent created through the real API and ACTIVATED as a real
    activation leaves one -- including the approved unattended set (D11)."""
    from harkeniq_cc.agent_activation import activation_grants_unattended

    body = {
        "name": f"s3e1-{'auto' if autonomous else 'human'}",
        "scopes": [{"scope_type": "site", "scope_ref": stack.sites[k]} for k in keys],
        "capabilities": [{"kind": "action_class", "capability_ref": "SEL_CLEAR"}],
        "autonomy_ceiling": 2 if autonomous else 0,
        "require_approval_always": not autonomous,
    }
    async with stack.client() as c:
        res = await c.post("/api/operational-agents/", json=body)
    assert res.status_code == 201, res.text
    agent_id = res.json()["id"]
    async with stack.sessionmaker() as session:
        agent = await session.get(CCOperationalAgent, agent_id)
        durable = activation_grants_unattended(agent, ["SEL_CLEAR"], 2)
        await record_activation(
            session, tenant_id=TENANT, agent=agent,
            unattended=durable if approve_activation else [],
        )
        await session.commit()
    return agent_id


async def _admit(stack) -> CCAgentProposal:
    created = await agent_runtime.evaluate_agents(stack.state, TENANT)
    assert len(created) == 1, f"expected one proposal, got {len(created)}"
    return created[0]


async def _row(stack, pid) -> CCAgentProposal:
    async with stack.sessionmaker() as session:
        return await session.get(CCAgentProposal, pid)


async def _records(stack, pid):
    async with stack.sessionmaker() as session:
        rows = (await session.execute(
            sa.select(CCApprovalRecord).where(CCApprovalRecord.subject_ref == pid)
        )).scalars().all()
        return sorted((r.approver_ref, r.decision) for r in rows)


async def _approve_undelivered(stack, pid) -> dict:
    """A named human approves while everything is authorized, and the site
    is briefly unreachable -- so the decision is RECORDED and left `approved`
    for the background pass, exactly as production leaves it."""
    FakeSM.raise_ = True
    async with stack.client() as c:
        res = await c.post(f"/api/approvals/{pid}/approve")
    FakeSM.raise_ = False
    assert res.status_code == 200, res.text
    assert res.json()["delivery"]["delivered"] is False
    assert (await _row(stack, pid)).status == "approved"
    return res.json()


# ---------------------------------------------------------------------------
# 1. The global gate at dispatch: E, F, M, N -- on BOTH paths
# ---------------------------------------------------------------------------


class TestTheGateAtDispatch:
    async def test_N_the_empty_registry_dispatches_as_before(self, _world, monkeypatch):
        monkeypatch.setattr(G, "_ACTIVE", ())
        stack = await _estate(await _stack())
        await _agent(stack)
        proposal = await _admit(stack)
        assert proposal.authorization_basis == "autonomous_grant"
        dispatched = await agent_runtime.dispatch_decided(stack.state, TENANT)
        assert [p.id for p in dispatched] == [proposal.id]
        assert len(FakeSM.calls) == 1

    async def test_F_autonomous_grant_is_withheld_and_keeps_its_mode(self, _world):
        stack = await _estate(await _stack())
        await _agent(stack)
        proposal = await _admit(stack)
        _world.on = True
        assert await agent_runtime.dispatch_decided(stack.state, TENANT) == []
        assert FakeSM.calls == []
        row = await _row(stack, proposal.id)
        assert row.status == "approved"                     # not failed
        assert row.authorization_basis == "autonomous_grant"  # not converted
        assert row.decided_by.startswith("autonomy:")        # not erased
        assert row.dispatch_reason == G.GLOBAL_WITHHELD_REASON

    async def test_E_F_human_approval_withheld_then_released_exactly_once(self, _world):
        stack = await _estate(await _stack())
        await _agent(stack, autonomous=False)
        proposal = await _admit(stack)
        assert proposal.status == "awaiting_approval"
        await _approve_undelivered(stack, proposal.id)
        ledger = await _records(stack, proposal.id)
        assert ledger == [(OWNER[0], "approved")]

        _world.on = True                                   # F: appears after approval
        for _ in range(3):
            assert await agent_runtime.dispatch_decided(stack.state, TENANT) == []
        assert FakeSM.calls == []
        row = await _row(stack, proposal.id)
        assert (row.status, row.decided_by) == ("approved", OWNER[1])
        assert row.dispatch_reason == G.GLOBAL_WITHHELD_REASON
        assert await _records(stack, proposal.id) == ledger  # ledger untouched

        _world.on = False                                  # E: clears -- no re-approval
        dispatched = await agent_runtime.dispatch_decided(stack.state, TENANT)
        assert [p.id for p in dispatched] == [proposal.id]
        assert len(FakeSM.calls) == 1
        assert FakeSM.calls[0]["authorization"] == "human_approval"
        await agent_runtime.dispatch_decided(stack.state, TENANT)
        assert len(FakeSM.calls) == 1                      # exactly one directive
        assert await _records(stack, proposal.id) == ledger

    async def test_the_synchronous_path_keeps_its_terminal_failure(self, _world):
        """D6 preserved: a human approving while the gate is held fails the
        proposal in the request, and nothing reaches the site."""
        stack = await _estate(await _stack())
        await _agent(stack, autonomous=False)
        proposal = await _admit(stack)
        _world.on = True
        async with stack.client() as c:
            res = await c.post(f"/api/approvals/{proposal.id}/approve")
        assert res.status_code == 200, res.text
        assert res.json()["delivery"] == {
            "accepted": False, "delivered": False,
            "reason": G.GLOBAL_WITHHELD_REASON,
        }
        assert FakeSM.calls == []
        assert (await _row(stack, proposal.id)).status == "failed"

    async def test_M_a_member_that_raises_fails_closed(self, _world):
        stack = await _estate(await _stack())
        await _agent(stack)
        proposal = await _admit(stack)
        _world.raises = True
        assert await agent_runtime.dispatch_decided(stack.state, TENANT) == []
        row = await _row(stack, proposal.id)
        assert row.dispatch_reason == G.GLOBAL_WITHHELD_REASON
        assert "SECRET" not in row.dispatch_reason and "HIDDEN" not in row.dispatch_reason

    async def test_one_audit_entry_per_distinct_cause(self, _world):
        from harkeniq_cc.db.models import CCAuditLog

        stack = await _estate(await _stack())
        await _agent(stack)
        await _admit(stack)
        _world.on = True
        for _ in range(4):
            await agent_runtime.dispatch_decided(stack.state, TENANT)
        async with stack.sessionmaker() as session:
            withheld = (await session.execute(
                sa.select(CCAuditLog).where(
                    CCAuditLog.action == "agent_proposal.dispatch_withheld")
            )).scalars().all()
        assert len(withheld) == 1
        assert withheld[0].detail["reason"] == G.GLOBAL_WITHHELD_REASON[:200]


# ---------------------------------------------------------------------------
# 2. Existing gates keep their own attribution: G, H, I
# ---------------------------------------------------------------------------


class TestAttributionOrder:
    async def _held(self, _world):
        stack = await _estate(await _stack())
        agent_id = await _agent(stack)
        proposal = await _admit(stack)
        _world.on = True            # the gate is ALSO holding it
        return stack, agent_id, proposal

    async def test_G_a_revoked_scope_is_named_before_the_gate(self, _world):
        stack, agent_id, proposal = await self._held(_world)
        async with stack.sessionmaker() as session:
            for grant in (await session.execute(sa.select(CCScopeGrant).where(
                    CCScopeGrant.principal_ref == agent_id))).scalars():
                grant.revoked_at = datetime.now(timezone.utc)
            await session.commit()
        await agent_runtime.dispatch_decided(stack.state, TENANT)
        assert "no longer reaches" in (await _row(stack, proposal.id)).dispatch_reason

    async def test_H_a_pause_is_named_before_the_gate(self, _world):
        stack, agent_id, proposal = await self._held(_world)
        async with stack.sessionmaker() as session:
            agent = await session.get(CCOperationalAgent, agent_id)
            agent.paused_reason = "held for the DC move"
            await session.commit()
        await agent_runtime.dispatch_decided(stack.state, TENANT)
        assert "paused" in (await _row(stack, proposal.id)).dispatch_reason

    async def test_I_a_removed_binding_is_named_before_the_gate(self, _world):
        stack, agent_id, proposal = await self._held(_world)
        async with stack.sessionmaker() as session:
            await session.execute(sa.delete(CCAgentCapability).where(
                CCAgentCapability.agent_id == agent_id,
                CCAgentCapability.kind == "action_class"))
            await session.commit()
        await agent_runtime.dispatch_decided(stack.state, TENANT)
        assert "no longer bound" in (await _row(stack, proposal.id)).dispatch_reason


# ---------------------------------------------------------------------------
# 3. D11 at dispatch (J), the site-local re-check (L), the node (K)
# ---------------------------------------------------------------------------


class TestFinalEligibility:
    async def test_J_an_unattended_class_no_activation_approved_is_withheld(self):
        stack = await _estate(await _stack())
        agent_id = await _agent(stack)
        proposal = await _admit(stack)
        assert proposal.authorization_basis == "autonomous_grant"
        # Re-activation approved NOTHING unattended: the set is recovered
        # from the newer activation, so SEL_CLEAR is no longer in it.
        async with stack.sessionmaker() as session:
            agent = await session.get(CCOperationalAgent, agent_id)
            await record_activation(
                session, tenant_id=TENANT, agent=agent, unattended=[],
                at=datetime.now(timezone.utc) + timedelta(seconds=5),
            )
            await session.commit()
        assert await agent_runtime.dispatch_decided(stack.state, TENANT) == []
        row = await _row(stack, proposal.id)
        assert row.dispatch_reason == UNATTENDED_NOT_APPROVED_REASON
        assert (row.status, row.authorization_basis) == ("approved", "autonomous_grant")

    async def test_J_a_human_decision_is_not_a_delegation(self):
        stack = await _estate(await _stack())
        await _agent(stack, autonomous=False, approve_activation=False)
        proposal = await _admit(stack)
        await _approve_undelivered(stack, proposal.id)
        assert [p.id for p in await agent_runtime.dispatch_decided(
            stack.state, TENANT)] == [proposal.id]

    async def test_L_a_stale_target_report_withholds_unattended_work(self):
        stack = await _estate(await _stack())
        await _agent(stack)
        proposal = await _admit(stack)
        stale = datetime.now(timezone.utc) - timedelta(hours=2)
        await _safety(stack, "A", as_of=stale, ingested_at=stale)
        assert await agent_runtime.dispatch_decided(stack.state, TENANT) == []
        assert (await _row(stack, proposal.id)).dispatch_reason == SITE_LOCAL_WITHHELD_REASON

    async def test_L_a_stale_report_does_not_stop_a_human_decision(self):
        """Not reported is REQUIRES_APPROVAL, and a human approved it."""
        stack = await _estate(await _stack())
        await _agent(stack, autonomous=False)
        proposal = await _admit(stack)
        await _approve_undelivered(stack, proposal.id)
        stale = datetime.now(timezone.utc) - timedelta(hours=2)
        await _safety(stack, "A", as_of=stale, ingested_at=stale)
        assert [p.id for p in await agent_runtime.dispatch_decided(
            stack.state, TENANT)] == [proposal.id]

    async def test_a_target_halted_after_approval_is_withheld_on_both_bases(self):
        stack = await _estate(await _stack())
        await _agent(stack, autonomous=False)
        proposal = await _admit(stack)
        await _approve_undelivered(stack, proposal.id)
        await _safety(stack, "A", sm_stop_switch=True)
        assert await agent_runtime.dispatch_decided(stack.state, TENANT) == []
        row = await _row(stack, proposal.id)
        assert (row.status, row.dispatch_reason) == ("approved", SITE_LOCAL_WITHHELD_REASON)
        await _safety(stack, "A", sm_stop_switch=False)          # the halt lifts
        assert len(await agent_runtime.dispatch_decided(stack.state, TENANT)) == 1

    async def test_a_hidden_site_changing_withholds_nothing(self):
        """B drops back, halts and goes dark after A's proposal was admitted:
        A's unattended grant is A's, and it dispatches."""
        stack = await _estate(await _stack())
        await _agent(stack)
        proposal = await _admit(stack)
        await _safety(stack, "B", sm_stop_switch=True, reported=False,
                      error_budgets=[{"action_type": "SEL_CLEAR", "dropped_back": True,
                                      "total_count": 9, "success_count": 1,
                                      "failure_count": 8}],
                      site_budgets={"SEL_CLEAR": 0})
        assert [p.id for p in await agent_runtime.dispatch_decided(
            stack.state, TENANT)] == [proposal.id]

    async def test_K_the_node_refuses_on_its_own(self):
        stack = await _estate(await _stack())
        await _agent(stack)
        proposal = await _admit(stack)
        FakeSM.accepted, FakeSM.reason = False, "not in this node's allow list"
        assert await agent_runtime.dispatch_decided(stack.state, TENANT) == []
        assert len(FakeSM.calls) == 1       # CC said eligible and delivered it
        row = await _row(stack, proposal.id)
        assert (row.status, row.dispatch_reason) == (
            "failed", "not in this node's allow list")


# ---------------------------------------------------------------------------
# 4. Admission over the database: the target decides, B does not
# ---------------------------------------------------------------------------


class TestAdmissionOverTheDatabase:
    async def test_a_hidden_drop_back_no_longer_routes_a_to_a_human(self):
        stack = await _estate(await _stack())
        await _safety(stack, "B", error_budgets=[{
            "action_type": "SEL_CLEAR", "dropped_back": True, "total_count": 9,
            "success_count": 1, "failure_count": 8}], site_budgets={"SEL_CLEAR": 0})
        await _agent(stack)
        proposal = await _admit(stack)
        assert (proposal.status, proposal.authorization_basis) == (
            "approved", "autonomous_grant")
        assert stack.sites["B"] not in str(proposal.blocking_conditions)

    async def test_F6_an_activation_that_approved_nothing_delegates_nothing(self):
        stack = await _estate(await _stack())
        await _agent(stack, approve_activation=False)
        proposal = await _admit(stack)
        assert proposal.status == "awaiting_approval"
        assert any(r["code"] == "agent_unattended_not_approved"
                   for r in proposal.blocking_conditions)


# ---------------------------------------------------------------------------
# 5. D11's recovery: the approved set is the one a human approved
# ---------------------------------------------------------------------------


class TestActivationApprovedSet:
    async def _agent_row(self, stack, **kw):
        agent_id = await _agent(stack, **kw)
        async with stack.sessionmaker() as session:
            return agent_id, await session.get(CCOperationalAgent, agent_id)

    async def _approved(self, stack, agent_id):
        from harkeniq_cc.agent_lifecycle import activation_approved_unattended

        async with stack.sessionmaker() as session:
            agent = await session.get(CCOperationalAgent, agent_id)
            return await activation_approved_unattended(session, TENANT, agent)

    async def test_the_recorded_set_is_recovered(self):
        stack = await _estate(await _stack())
        agent_id, _ = await self._agent_row(stack)
        assert await self._approved(stack, agent_id) == frozenset({"SEL_CLEAR"})

    async def test_a_preflight_run_after_activation_changes_nothing(self):
        stack = await _estate(await _stack())
        agent_id, _ = await self._agent_row(stack)
        async with stack.sessionmaker() as session:
            session.add(CCAgentPreflight(
                agent_id=agent_id, tenant_id=TENANT, configuration_version=1,
                overall="warn", requires_activation_approval=True,
                result={"unattended_classes": ["SEL_CLEAR", "BMC_RESET"]},
                produced_at=datetime.now(timezone.utc) + timedelta(minutes=1),
            ))
            await session.commit()
        assert await self._approved(stack, agent_id) == frozenset({"SEL_CLEAR"})

    async def test_an_agent_activated_before_a2_approved_nothing(self):
        stack = await _estate(await _stack())
        agent_id, _ = await self._agent_row(stack)
        async with stack.sessionmaker() as session:
            agent = await session.get(CCOperationalAgent, agent_id)
            agent.activated_version = 0
            await session.commit()
        assert await self._approved(stack, agent_id) == frozenset()

    @pytest.mark.parametrize("status", ["paused", "draft", "retired"])
    async def test_an_agent_that_is_not_active_delegates_nothing(self, status):
        stack = await _estate(await _stack())
        agent_id, _ = await self._agent_row(stack)
        async with stack.sessionmaker() as session:
            (await session.get(CCOperationalAgent, agent_id)).status = status
            await session.commit()
        assert await self._approved(stack, agent_id) == frozenset()

    async def test_a_denied_activation_approved_nothing(self):
        stack = await _estate(await _stack())
        agent_id = await _agent(stack, approve_activation=False)
        async with stack.sessionmaker() as session:
            agent = await session.get(CCOperationalAgent, agent_id)
            await record_activation(
                session, tenant_id=TENANT, agent=agent, unattended=["SEL_CLEAR"],
                approved=False, at=datetime.now(timezone.utc) + timedelta(seconds=5),
            )
            await session.commit()
        assert await self._approved(stack, agent_id) == frozenset()

    async def test_dual_approval_needs_both(self):
        """E0.1's ONE completion rule decides, not a record count."""
        stack = await _estate(await _stack())
        async with stack.client() as c:
            res = await c.post("/api/policies/", json={
                "name": "dual", "action_type": "*", "device_type": "*",
                "risk_level": "*", "required_approvers": 2,
            })
            assert res.status_code == 200, res.text
        agent_id, _ = await self._agent_row(stack)
        assert await self._approved(stack, agent_id) == frozenset()

    async def test_F6_the_preflight_asks_for_approval_whatever_the_safety_picture(self):
        """D11: the unattended set is durable configuration. A drop-back at a
        site the agent reaches no longer empties it, so activation that
        confers unattended execution is always put to a human."""
        stack = await _estate(await _stack())
        await _safety(stack, "A", error_budgets=[{
            "action_type": "SEL_CLEAR", "dropped_back": True, "total_count": 9,
            "success_count": 1, "failure_count": 8}])
        body = {
            "name": "f6", "autonomy_ceiling": 2, "require_approval_always": False,
            "scopes": [{"scope_type": "site", "scope_ref": stack.sites["A"]}],
            "capabilities": [{"kind": "action_class", "capability_ref": "SEL_CLEAR"}],
        }
        async with stack.client() as c:
            agent_id = (await c.post("/api/operational-agents/", json=body)).json()["id"]
            result = (await c.post(f"/api/operational-agents/{agent_id}/preflight")).json()
        assert result["unattended_classes"] == ["SEL_CLEAR"]
        assert result["requires_activation_approval"] is True


# ---------------------------------------------------------------------------
# 6. The preflight's R5
# ---------------------------------------------------------------------------


class TestPreflightR5:
    async def _safety_dimension(self, stack) -> dict:
        body = {
            "name": "r5", "scopes": [{"scope_type": "site", "scope_ref": stack.sites["A"]}],
            "capabilities": [{"kind": "action_class", "capability_ref": "SEL_CLEAR"}],
        }
        async with stack.client() as c:
            agent_id = (await c.post("/api/operational-agents/", json=body)).json()["id"]
            result = (await c.post(f"/api/operational-agents/{agent_id}/preflight")).json()
        return next(d for d in result["dimensions"] if d["dimension"] == "safety")

    async def test_a_reached_site_that_has_not_reported_is_unknown(self):
        """Site B reporting used to turn this READY (the one non-monotone
        input). The agent reaches only A, and A has not reported."""
        stack = await _estate(await _stack())
        await _safety(stack, "A", reported=False)
        assert (await self._safety_dimension(stack))["verdict"] == "unknown"

    async def test_every_reached_site_reporting_is_ready(self):
        stack = await _estate(await _stack())
        assert (await self._safety_dimension(stack))["verdict"] == "ready"
