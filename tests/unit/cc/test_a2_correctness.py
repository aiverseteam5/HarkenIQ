"""A2 correctness: the five defects the recovery review found, pinned.

Every one of these was a value that was declared, modelled, migrated and
READ -- and never written, or never called. So each test here asserts the
CALL, not only the judgement: a pure function that returns the right
answer proves nothing about whether the runtime ever asks it.

  D-1  activation approval counted any single record as approval, so a
       tenant configuring `required_approvers = 2` got ONE
  D-2  `activated_version` had no writer, so every active agent reported
       configuration drift from the moment it was switched on
  D-3  the per-agent execution budget was reportable and unenforced
  D-4  `install_bound_skills` was complete and invoked by nothing
  D-5  budget and pause were columns with no way to set them
"""

from __future__ import annotations

import httpx
import pytest

from harkeniq_cc import agent_runtime, skill_fetch, sm_client
from harkeniq_cc.api import approvals as approvals_api
from harkeniq_cc.api.deps import get_current_user
from harkeniq_cc.app import create_app
from harkeniq_cc.auth import ROLE_PERMISSIONS, UserContext, configure_auth
from harkeniq_cc.config import CCConfig
from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_cc.db.models import (
    CCAutonomyBudget,
    CCFleetCache,
    CCIncident,
    CCOutcomeHistory,
    CCSite,
)
from harkeniq_cc.db.repos import AgentProposalRepo, OperationalAgentRepo
from harkeniq_cc.runtime import AppState

TENANT = "t1"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class FakeSM:
    """Records what was sent to the site. Never touches a network."""

    dispatches: list = []
    installs: list = []
    accepted = True

    def __init__(self, *_a, **_kw):
        pass

    async def dispatch_action(self, endpoint, token, **kw):
        FakeSM.dispatches.append(kw)
        return {"accepted": FakeSM.accepted,
                "directive_id": f"dir-{len(FakeSM.dispatches)}", "reason": ""}

    async def route_approval(self, **kw):
        FakeSM.dispatches.append(kw)
        return {"accepted": True, "delivered": True, "reason": ""}

    async def install_skill(self, endpoint, token, **kw):
        FakeSM.installs.append(kw)
        return {"accepted": True, "queued": len(kw.get("device_agent_ids") or []),
                "reason": ""}


class _FakeAction:
    def __init__(self, type_):
        self.type = type_


class _FakeRule:
    def __init__(self, type_):
        self.action = _FakeAction(type_)


class FakeSkill:
    """What `parse_skill` returns, in the shape the validator reads.

    `skill_recommended_actions` walks `rules[].action.type` deliberately
    -- from the PARSED definition, so a skill cannot declare one thing
    alongside itself and recommend another.
    """

    def __init__(self, name="fan-health", recommends=("SEL_CLEAR",)):
        self.name = name
        self.version = "1"
        self.raw_yaml = f"name: {name}\n"
        self.rules = [_FakeRule(a) for a in recommends]


@pytest.fixture(autouse=True)
def _fakes(monkeypatch):
    FakeSM.dispatches = []
    FakeSM.installs = []
    FakeSM.accepted = True
    monkeypatch.setattr(agent_runtime, "SMClient", FakeSM)
    monkeypatch.setattr(approvals_api, "SMClient", FakeSM)
    monkeypatch.setattr(sm_client, "SMClient", FakeSM)
    yield


class Stack:
    """One Central Command that can answer as several different people."""

    def __init__(self, app, state):
        self.app = app
        self.state = state
        self.sessionmaker = state.sessionmaker
        self.persona = ("kc-owner", "owner@example.com", "tenant_owner")

    def as_person(self, sub, email, role="tenant_owner"):
        self.persona = (sub, email, role)
        return self

    def client(self):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test",
        )


async def _stack() -> Stack:
    config = CCConfig(tenant_id=TENANT, insecure=True)
    configure_auth("", "", "", insecure=True)
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    sessionmaker = make_sessionmaker(engine)
    state = AppState(config=config, engine=engine, sessionmaker=sessionmaker)
    app = create_app(state)
    stack = Stack(app, state)

    async def _fake():
        sub, email, role = stack.persona
        return UserContext(
            user_id=sub, email=email, tenant_id=TENANT, role=role,
            permissions=list(ROLE_PERMISSIONS[role]),
        )

    app.dependency_overrides[get_current_user] = _fake
    return stack


async def _seed(stack: Stack, *, level: int = 2, incident: bool = True) -> str:
    """A site, a declared device, a tenant autonomy level, an incident."""
    async with stack.sessionmaker() as session:
        site = CCSite(
            tenant_id=TENANT, site_name="DC-1",
            sm_endpoint="sm:50051", sm_token="tok",
        )
        session.add(site)
        await session.flush()
        session.add(CCFleetCache(
            site_id=site.id, agent_id="node-1", agent_name="rack1-node1",
            vendor="Dell", model="R750", device_class="server",
            observation="observed", health="Critical",
            # Declared, so the preflight has no UNKNOWN to acknowledge and
            # the skill's recommended action is provably reachable.
            # `reach_known` is what separates "declared nothing" from
            # "has not declared" (A17.4) -- without it this device reads
            # as UNKNOWN, not as incapable.
            capabilities={
                "reach_known": True,
                "implemented": ["SEL_CLEAR", "COLLECT_DIAGNOSTICS"],
                "allowed": ["SEL_CLEAR", "COLLECT_DIAGNOSTICS"],
                "effective": ["SEL_CLEAR", "COLLECT_DIAGNOSTICS"],
            },
        ))
        session.add(CCAutonomyBudget(
            tenant_id=TENANT, device_type="*", level=level,
            budget_limit=100, budget_period="daily",
        ))
        if incident:
            session.add(CCIncident(
                incident_id="inc-1", tenant_id=TENANT, site_id=site.id,
                kind="device", status="open", title="BMC event log saturated",
                device_agent_id="node-1", subsystem="log", confidence=0.9,
            ))
        await session.commit()
        return site.id


def _body(site_id, **kw):
    body = {
        "name": "Night Shift",
        "scopes": [{"scope_type": "site", "scope_ref": site_id}],
        "capabilities": [{"kind": "action_class", "capability_ref": "SEL_CLEAR"}],
    }
    body.update(kw)
    return body


async def _unattended_agent(client, site_id, **kw):
    """An agent whose activation WOULD confer unattended execution.

    Ceiling 2 with the tenant at level 2 makes SEL_CLEAR `autonomous`,
    and no blanket human requirement -- which is precisely the condition
    D1 says must be approved before it may be switched on.
    """
    body = _body(site_id, autonomy_ceiling=2, require_approval_always=False)
    body.update(kw)
    res = await client.post("/api/operational-agents/", json=body)
    assert res.status_code == 201, res.text
    return res.json()["id"]


async def _preflight(client, agent_id) -> dict:
    res = await client.post(f"/api/operational-agents/{agent_id}/preflight")
    assert res.status_code == 200, res.text
    result = res.json()
    if result.get("requires_acknowledgement"):
        ack = await client.post(f"/api/operational-agents/{agent_id}/acknowledge")
        assert ack.status_code == 200, ack.text
    return result


async def _subject(client, agent_id) -> str:
    detail = await client.get(f"/api/operational-agents/{agent_id}")
    return detail.json()["agent"]["activation_subject_ref"]


async def _dual_policy(client):
    res = await client.post("/api/policies/", json={
        "name": "dual", "action_type": "*", "device_type": "*",
        "risk_level": "*", "required_approvers": 2,
    })
    assert res.status_code == 200, res.text
    return res.json()["policy"]


# ---------------------------------------------------------------------------
# D-1 — one ledger, ONE completion rule
# ---------------------------------------------------------------------------


class TestActivationApprovalUsesTheOneCompletionRule:
    """A tenant that configures dual approval must GET dual approval.

    The first version of A2 read approval records directly and treated
    any single record as approval, so activation -- the moment real
    unattended authority is conferred -- was the one decision in the
    platform that a dual-approval policy did not bind. That is E0.1's own
    defect at a fourth origin.
    """

    @pytest.mark.asyncio
    async def test_activation_that_confers_unattended_needs_approval_at_all(self):
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            agent_id = await _unattended_agent(c, site_id)
            result = await _preflight(c, agent_id)
            assert result["requires_activation_approval"] is True
            assert "SEL_CLEAR" in result["unattended_classes"]
            # And the subject is raised so an operator can see the
            # decision before attempting the transition.
            assert await _subject(c, agent_id)

    @pytest.mark.asyncio
    async def test_one_approval_under_a_dual_policy_leaves_activation_pending(self):
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            await _dual_policy(c)
            agent_id = await _unattended_agent(c, site_id)
            await _preflight(c, agent_id)
            subject = await _subject(c, agent_id)

            stack.as_person("kc-a", "a@example.com", "operator")
            first = await c.post(f"/api/approvals/{subject}/approve")
            assert first.status_code == 200, first.text
            assert first.json()["approval"]["remaining"] == 1

            # THE defect. One approval is not the two the tenant configured.
            stack.as_person("kc-owner", "owner@example.com", "tenant_owner")
            res = await c.post(f"/api/operational-agents/{agent_id}/activate")
            assert res.status_code == 409, res.text
            assert "1 of 2 approval(s) recorded" in res.json()["detail"]

            agent = await c.get(f"/api/operational-agents/{agent_id}")
            assert agent.json()["agent"]["status"] == "draft"

    @pytest.mark.asyncio
    async def test_activation_succeeds_once_the_required_count_is_met(self):
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            await _dual_policy(c)
            agent_id = await _unattended_agent(c, site_id)
            await _preflight(c, agent_id)
            subject = await _subject(c, agent_id)

            stack.as_person("kc-a", "a@example.com", "operator")
            await c.post(f"/api/approvals/{subject}/approve")
            stack.as_person("kc-b", "b@example.com", "operator")
            second = await c.post(f"/api/approvals/{subject}/approve")
            assert second.json()["decision"] == "approved"

            stack.as_person("kc-owner", "owner@example.com", "tenant_owner")
            res = await c.post(f"/api/operational-agents/{agent_id}/activate")
            assert res.status_code == 200, res.text
            assert res.json()["status"] == "active"
            assert res.json()["activated_version"] == 1

    @pytest.mark.asyncio
    async def test_a_denial_is_terminal_for_activation(self):
        """D16: denied is final, and it outranks any number of approvals."""
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            await _dual_policy(c)
            agent_id = await _unattended_agent(c, site_id)
            await _preflight(c, agent_id)
            subject = await _subject(c, agent_id)

            stack.as_person("kc-a", "a@example.com", "operator")
            await c.post(f"/api/approvals/{subject}/approve")
            stack.as_person("kc-b", "b@example.com", "operator")
            await c.post(f"/api/approvals/{subject}/deny")

            stack.as_person("kc-owner", "owner@example.com", "tenant_owner")
            res = await c.post(f"/api/operational-agents/{agent_id}/activate")
            assert res.status_code == 409
            assert "denied" in res.json()["detail"]

    @pytest.mark.asyncio
    async def test_a_single_approver_policy_still_activates_on_one(self):
        """The fix must not silently raise the bar for everyone else."""
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            agent_id = await _unattended_agent(c, site_id)
            await _preflight(c, agent_id)
            subject = await _subject(c, agent_id)
            stack.as_person("kc-a", "a@example.com", "operator")
            assert (
                await c.post(f"/api/approvals/{subject}/approve")
            ).status_code == 200
            stack.as_person("kc-owner", "owner@example.com", "tenant_owner")
            res = await c.post(f"/api/operational-agents/{agent_id}/activate")
            assert res.status_code == 200, res.text

    @pytest.mark.asyncio
    async def test_a_propose_only_agent_needs_no_activation_approval(self):
        """D1 is derived: no unattended grant, no ceremony."""
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            await _dual_policy(c)
            res = await c.post("/api/operational-agents/", json=_body(site_id))
            agent_id = res.json()["id"]
            result = await _preflight(c, agent_id)
            assert result["requires_activation_approval"] is False
            assert (
                await c.post(f"/api/operational-agents/{agent_id}/activate")
            ).status_code == 200

    @pytest.mark.asyncio
    async def test_the_gate_calls_the_one_completion_rule(self):
        """Asserted structurally, not only behaviourally.

        A second implementation of "is this approved" is a defect
        whatever it computes, so this pins that the activation gate
        DELEGATES rather than deciding for itself.
        """
        import inspect

        from harkeniq_cc.api.operational_agents import _activation_decision

        source = inspect.getsource(_activation_decision)
        assert "activation_approval_state" in source
        # It must not go back to reading records and judging them.
        assert "list_for_subject" not in source
        assert 'decision == "denied"' not in source


# ---------------------------------------------------------------------------
# D-2 — activated_version, and the drift invariant
# ---------------------------------------------------------------------------


class TestActivatedVersion:
    """`active AND activated_version == version` -> no drift."""

    @pytest.mark.asyncio
    async def test_activation_records_the_version_it_switched_on(self):
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            res = await c.post("/api/operational-agents/", json=_body(site_id))
            agent_id = res.json()["id"]
            await _preflight(c, agent_id)
            await c.post(f"/api/operational-agents/{agent_id}/activate")

        async with stack.sessionmaker() as session:
            agent = await OperationalAgentRepo(session).get(TENANT, agent_id)
            assert agent.status == "active"
            assert agent.activated_version == agent.version == 1

    @pytest.mark.asyncio
    async def test_a_freshly_activated_agent_reports_no_drift(self):
        """The defect, stated as the user would see it.

        `version` starts at 1 and `activated_version` defaulted to 0, so
        `0 != 1` made every active agent report drift immediately.
        """
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            res = await c.post("/api/operational-agents/", json=_body(site_id))
            agent_id = res.json()["id"]
            await _preflight(c, agent_id)
            await c.post(f"/api/operational-agents/{agent_id}/activate")

            view = (await c.get(f"/api/operational-agents/{agent_id}")).json()
            assert view["agent"]["activated_version"] == 1
            assert view["agent"]["configuration_drifted"] is False

            runtime = (
                await c.get(f"/api/operational-agents/{agent_id}/runtime")
            ).json()
            assert runtime["activated_version"] == 1
            assert runtime["configuration_drifted"] is False

    @pytest.mark.asyncio
    async def test_editing_an_active_agent_does_report_drift(self):
        """The invariant has to hold in BOTH directions to mean anything."""
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            res = await c.post("/api/operational-agents/", json=_body(site_id))
            agent_id = res.json()["id"]
            await _preflight(c, agent_id)
            await c.post(f"/api/operational-agents/{agent_id}/activate")

            edited = await c.patch(
                f"/api/operational-agents/{agent_id}",
                json={"description": "now covers the day shift too"},
            )
            assert edited.status_code == 200

            view = (await c.get(f"/api/operational-agents/{agent_id}")).json()
            assert view["agent"]["version"] == 2
            assert view["agent"]["activated_version"] == 1
            assert view["agent"]["configuration_drifted"] is True
            # And the stale preflight refuses a re-activation attempt.
            runtime = (
                await c.get(f"/api/operational-agents/{agent_id}/runtime")
            ).json()
            assert runtime["preflight"]["current"] is False

    @pytest.mark.asyncio
    async def test_a_draft_agent_is_not_drifted(self):
        """Drift is a statement about a RUNNING configuration."""
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            res = await c.post("/api/operational-agents/", json=_body(site_id))
            view = (
                await c.get(f"/api/operational-agents/{res.json()['id']}")
            ).json()
            assert view["agent"]["status"] == "draft"
            assert view["agent"]["configuration_drifted"] is False
            assert view["agent"]["activation_provenance"] == "inactive"

    @pytest.mark.asyncio
    async def test_an_agent_activated_before_a2_reads_unknown_not_drifted(self):
        """Found on real PostgreSQL, by upgrading a pre-A2 deployment.

        An agent that was active before A2 recorded activation versions
        carries `activated_version = 0`, and `version` starts at 1 -- so a
        naive comparison shouts DRIFT at every existing agent the moment a
        customer upgrades. That asserts a fact the platform does not have.

        Backfilling the column in the migration would be the same error
        pointing the other way: it would claim these agents are running
        their current configuration, which nobody checked. Unknown is not
        zero, and it is not a guess.
        """
        from harkeniq_cc.db.models import CCOperationalAgent

        stack = await _stack()
        await _seed(stack)
        async with stack.sessionmaker() as session:
            session.add(CCOperationalAgent(
                id="legacy", tenant_id=TENANT, name="Pre-A2 Agent",
                description="activated before A2 existed",
                version=3, status="active", activated_version=0,
                autonomy_ceiling=2, require_approval_always=False,
                created_by="vinod", updated_by="vinod", activated_by="vinod",
            ))
            await session.commit()

        async with stack.client() as c:
            view = (await c.get("/api/operational-agents/legacy")).json()["agent"]
            assert view["status"] == "active"
            assert view["activated_version"] == 0
            assert view["activation_provenance"] == "unknown"
            assert view["configuration_drifted"] is False, (
                "an upgraded pre-A2 agent must not be reported as drifted"
            )
            runtime = (
                await c.get("/api/operational-agents/legacy/runtime")
            ).json()
            assert runtime["activation_provenance"] == "unknown"
            assert runtime["configuration_drifted"] is False

    @pytest.mark.asyncio
    async def test_the_drift_rule_has_exactly_one_implementation(self):
        """Two copies of a reported rule diverge. This one had two."""
        import inspect

        from harkeniq_cc import agent_lifecycle, operational_agent

        for module in (operational_agent, agent_lifecycle):
            source = inspect.getsource(module)
            assert "activation_provenance(agent)" in source
            assert '"configuration_drifted": (' not in source, (
                f"{module.__name__} recomputes drift instead of asking "
                f"activation_provenance"
            )


# ---------------------------------------------------------------------------
# D-3 / D-5 — the budget is real, and settable
# ---------------------------------------------------------------------------


async def _record_executions(stack, site_id, actor: str, count: int):
    """Outcomes attributed to this agent -- what actually RAN."""
    async with stack.sessionmaker() as session:
        for i in range(count):
            session.add(CCOutcomeHistory(
                site_id=site_id, action_id=f"a{i}", action_type="SEL_CLEAR",
                device_agent_id="node-1", vendor="Dell", model="R750",
                outcome="SUCCESS", actor=actor,
            ))
        await session.commit()


async def _autonomous_proposal(stack, site_id, actor: str, agent_id: str,
                               basis="autonomous_grant", key="k1"):
    """An approved proposal, for an agent that is actually RUNNING.

    A5 (A22.12) re-checks current lifecycle at dispatch, so an agent left
    in `draft` here would be correctly refused -- a proposal attributed to
    an agent nobody ever switched on must not execute. In production only
    an active agent proposes at all (`EVALUATING_STATUSES`), so activating
    the row is what makes this shortcut match the runtime rather than
    weakening the gate to match the shortcut.
    """
    from harkeniq_cc.db.models import CCAgentProposal, CCOperationalAgent

    async with stack.sessionmaker() as session:
        agent = await session.get(CCOperationalAgent, agent_id)
        if agent is not None and agent.status == "draft":
            agent.status = "active"
            agent.activated_version = agent.version
        row = CCAgentProposal(
            tenant_id=TENANT, agent_id=agent_id, actor=actor, agent_version=1,
            site_id=site_id, device_agent_id="node-1", action_type="SEL_CLEAR",
            params={}, rationale="log saturated", evidence={},
            disposition="autonomous" if basis == "autonomous_grant"
            else "requires_approval",
            authorization_basis=basis, status="approved",
            decided_by="contract" if basis == "autonomous_grant" else "a@x.com",
            dedupe_key=f"{agent_id}:node-1:SEL_CLEAR:{key}",
        )
        session.add(row)
        await session.commit()
        return row.id


class TestBudgetIsEnforcedAndConfigurable:
    @pytest.mark.asyncio
    async def test_budget_is_settable_at_creation_and_on_update(self):
        """D-5: a budget a customer cannot set is not a product feature."""
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            res = await c.post("/api/operational-agents/", json=_body(
                site_id, execution_budget=5, budget_period="weekly",
            ))
            assert res.status_code == 201, res.text
            agent_id = res.json()["id"]
            view = (await c.get(f"/api/operational-agents/{agent_id}")).json()
            assert view["agent"]["execution_budget"] == 5
            assert view["agent"]["budget_period"] == "weekly"

            patched = await c.patch(
                f"/api/operational-agents/{agent_id}",
                json={"execution_budget": 2, "budget_period": "daily"},
            )
            assert patched.status_code == 200, patched.text
            view = (await c.get(f"/api/operational-agents/{agent_id}")).json()
            assert view["agent"]["execution_budget"] == 2
            assert view["agent"]["budget_period"] == "daily"
            # It is configuration, so it is version-bound like the rest.
            assert view["agent"]["version"] == 2

    @pytest.mark.asyncio
    async def test_an_unknown_budget_period_is_refused(self):
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            res = await c.post("/api/operational-agents/", json=_body(
                site_id, budget_period="fortnightly",
            ))
            assert res.status_code == 400
            assert "budget_period" in res.json()["detail"]

    @pytest.mark.asyncio
    async def test_pause_is_settable_and_does_not_bump_the_version(self):
        """Pause is a runtime safety control, not a configuration edit.

        Versioning it would mean an emergency pause invalidated a valid
        activation approval and resuming needed a fresh one -- a control
        that is expensive to use in an emergency does not get used.
        """
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            res = await c.post("/api/operational-agents/", json=_body(site_id))
            agent_id = res.json()["id"]
            paused = await c.patch(
                f"/api/operational-agents/{agent_id}",
                json={"paused_reason": "held by ops during the DC move"},
            )
            assert paused.status_code == 200, paused.text
            view = (await c.get(f"/api/operational-agents/{agent_id}")).json()
            assert view["agent"]["paused_reason"] == "held by ops during the DC move"
            assert view["agent"]["version"] == 1

            resumed = await c.patch(
                f"/api/operational-agents/{agent_id}", json={"paused_reason": ""},
            )
            assert resumed.status_code == 200
            view = (await c.get(f"/api/operational-agents/{agent_id}")).json()
            assert view["agent"]["paused_reason"] is None
            assert view["agent"]["version"] == 1

    @pytest.mark.asyncio
    async def test_exhausted_budget_withholds_unattended_execution(self):
        """D-3: the whole defect. The dispatch loop never asked.

        This drives `dispatch_decided`, the production unattended path --
        not `unattended_permitted`, which was already right and already
        tested, and which nothing called.
        """
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            agent_id = await _unattended_agent(c, site_id, execution_budget=2)

        actor = f"op-agent:{agent_id}@v1"
        await _record_executions(stack, site_id, actor, 2)  # budget spent
        await _autonomous_proposal(stack, site_id, actor, agent_id)

        dispatched = await agent_runtime.dispatch_decided(stack.state, TENANT)
        assert dispatched == []
        assert FakeSM.dispatches == [], "an exhausted budget dispatched anyway"

        # Withheld, not destroyed: it returns to the human queue.
        async with stack.sessionmaker() as session:
            rows = await AgentProposalRepo(session).list_by_status(
                TENANT, ["awaiting_approval"],
            )
            assert len(rows) == 1
            assert rows[0].authorization_basis == "human_approval"
            assert "execution budget" in rows[0].dispatch_reason

    @pytest.mark.asyncio
    async def test_a_budget_with_room_still_dispatches_unattended(self):
        """The refusal must be the budget, not the check itself."""
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            agent_id = await _unattended_agent(c, site_id, execution_budget=5)

        actor = f"op-agent:{agent_id}@v1"
        await _record_executions(stack, site_id, actor, 1)
        await _autonomous_proposal(stack, site_id, actor, agent_id)

        dispatched = await agent_runtime.dispatch_decided(stack.state, TENANT)
        assert len(dispatched) == 1
        assert len(FakeSM.dispatches) == 1

    @pytest.mark.asyncio
    async def test_no_budget_configured_dispatches_as_before(self):
        """0 means unset. The tenant and site budgets still apply."""
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            agent_id = await _unattended_agent(c, site_id)

        actor = f"op-agent:{agent_id}@v1"
        await _record_executions(stack, site_id, actor, 50)
        await _autonomous_proposal(stack, site_id, actor, agent_id)

        assert len(await agent_runtime.dispatch_decided(stack.state, TENANT)) == 1

    @pytest.mark.asyncio
    async def test_a_paused_agent_does_not_run_unattended(self):
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            agent_id = await _unattended_agent(c, site_id)
            await c.patch(f"/api/operational-agents/{agent_id}",
                          json={"paused_reason": "held by ops"})

        actor = f"op-agent:{agent_id}@v1"
        await _autonomous_proposal(stack, site_id, actor, agent_id)
        assert await agent_runtime.dispatch_decided(stack.state, TENANT) == []
        assert FakeSM.dispatches == []

    @pytest.mark.asyncio
    async def test_in_flight_dispatches_count_against_the_budget(self):
        """Otherwise a burst empties the budget of meaning.

        Outcome history lags dispatch, so counting only settled outcomes
        would let an agent with a budget of one dispatch every pending
        proposal in a single pass.
        """
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            agent_id = await _unattended_agent(c, site_id, execution_budget=1)

        actor = f"op-agent:{agent_id}@v1"
        await _autonomous_proposal(stack, site_id, actor, agent_id, key="k1")
        await _autonomous_proposal(stack, site_id, actor, agent_id, key="k2")

        # No outcomes have come back at all, so a settled-only count would
        # read zero and ship both.
        dispatched = await agent_runtime.dispatch_decided(stack.state, TENANT)
        assert len(dispatched) == 1
        assert len(FakeSM.dispatches) == 1

    @pytest.mark.asyncio
    async def test_editing_an_agent_does_not_refill_a_spent_budget(self):
        """Found building the A-K acceptance, on the live contract.

        Consumption was keyed to `op-agent:<id>@v<n>` -- the attribution
        string, which carries the version. So editing a DESCRIPTION
        bumped the version and reset a spent budget to zero: the one
        control a customer sets to bound unattended work was refilled by
        the most routine edit there is, and by the agent's own
        reconfiguration flow.

        Attribution still names the exact version on every outcome (D3
        requires it). Consumption belongs to the AGENT.
        """
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            agent_id = await _unattended_agent(c, site_id, execution_budget=2)
            await _record_executions(stack, site_id, f"op-agent:{agent_id}@v1", 2)

            runtime = (
                await c.get(f"/api/operational-agents/{agent_id}/runtime")
            ).json()
            assert runtime["budget"]["executions_used"] == 2
            assert runtime["budget"]["exhausted"] is True

            # An ordinary configuration edit.
            edited = await c.patch(
                f"/api/operational-agents/{agent_id}",
                json={"description": "same agent, new wording"},
            )
            assert edited.status_code == 200
            assert edited.json()["version"] == 2

            runtime = (
                await c.get(f"/api/operational-agents/{agent_id}/runtime")
            ).json()
            assert runtime["budget"]["executions_used"] == 2, (
                "an edit refilled the budget: work done under v1 is still "
                "this agent's work under v2"
            )
            assert runtime["budget"]["exhausted"] is True

        # And it still refuses unattended dispatch after the edit.
        await _autonomous_proposal(
            stack, site_id, f"op-agent:{agent_id}@v2", agent_id,
        )
        assert await agent_runtime.dispatch_decided(stack.state, TENANT) == []

    @pytest.mark.asyncio
    async def test_outcomes_keep_naming_the_version_that_decided_them(self):
        """The fix must not blur attribution, which is a different axis."""
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            agent_id = await _unattended_agent(c, site_id, execution_budget=5)
            await _record_executions(stack, site_id, f"op-agent:{agent_id}@v1", 1)
            await c.patch(f"/api/operational-agents/{agent_id}",
                          json={"description": "v2"})
            await _record_executions(stack, site_id, f"op-agent:{agent_id}@v2", 1)

        async with stack.sessionmaker() as session:
            from sqlalchemy import select

            from harkeniq_cc.db.models import CCOutcomeHistory

            actors = sorted(
                (await session.execute(select(CCOutcomeHistory.actor))).scalars().all()
            )
        assert actors == [f"op-agent:{agent_id}@v1", f"op-agent:{agent_id}@v2"], (
            "each outcome must still name the configuration that decided it"
        )

    @pytest.mark.asyncio
    async def test_budget_exhaustion_does_not_block_human_approved_work(self):
        """D2's other half, and the one that makes it a budget not a switch."""
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            agent_id = await _unattended_agent(c, site_id, execution_budget=1)
            await _preflight(c, agent_id)
            subject = await _subject(c, agent_id)
            await c.post(f"/api/approvals/{subject}/approve")
            assert (
                await c.post(f"/api/operational-agents/{agent_id}/activate")
            ).status_code == 200

            actor = f"op-agent:{agent_id}@v1"
            await _record_executions(stack, site_id, actor, 5)  # well over
            proposal_id = await _autonomous_proposal(
                stack, site_id, actor, agent_id, basis="human_approval",
            )
            async with stack.sessionmaker() as session:
                repo = AgentProposalRepo(session)
                row = await repo.get(TENANT, proposal_id)
                row.status = "awaiting_approval"
                await session.commit()

            res = await c.post(f"/api/approvals/{proposal_id}/approve")
            assert res.status_code == 200, res.text
            assert res.json()["delivery"]["accepted"] is True, res.text

    @pytest.mark.asyncio
    async def test_one_notion_of_executions_used(self):
        """Preflight, runtime and dispatch must never quote different numbers."""
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            agent_id = await _unattended_agent(c, site_id, execution_budget=10)
            actor = f"op-agent:{agent_id}@v1"
            await _record_executions(stack, site_id, actor, 3)
            await _autonomous_proposal(stack, site_id, actor, agent_id)
            await agent_runtime.dispatch_decided(stack.state, TENANT)

            result = await _preflight(c, agent_id)
            budget = next(
                d for d in result["dimensions"] if d["dimension"] == "budget"
            )
            runtime = (
                await c.get(f"/api/operational-agents/{agent_id}/runtime")
            ).json()
            # 3 settled + 1 in flight, reported identically by both.
            assert budget["used"] == 4
            assert runtime["budget"]["executions_used"] == 4


# ---------------------------------------------------------------------------
# D-4 — skills install on activation
# ---------------------------------------------------------------------------


class TestSkillInstallOnActivation:
    """`install_bound_skills` was complete and invoked by nothing."""

    #: What the marketplace skill recommends, per test. Read at fetch
    #: time so a test can change it without re-patching.
    recommends = ("SEL_CLEAR",)

    @pytest.fixture(autouse=True)
    def _fake_console(self, monkeypatch):
        TestSkillInstallOnActivation.recommends = ("SEL_CLEAR",)

        async def _fetch(state, tenant_id, skill_id):
            return FakeSkill(
                name=skill_id,
                recommends=TestSkillInstallOnActivation.recommends,
            ), ""

        monkeypatch.setattr(skill_fetch, "fetch_skill_definition", _fetch)
        yield

    async def _agent_with_skill(self, c, site_id):
        body = _body(site_id)
        body["capabilities"] = [
            {"kind": "action_class", "capability_ref": "SEL_CLEAR"},
            {"kind": "skill", "capability_ref": "fan-health"},
        ]
        res = await c.post("/api/operational-agents/", json=body)
        assert res.status_code == 201, res.text
        return res.json()["id"]

    @pytest.mark.asyncio
    async def test_activation_installs_bound_skills_onto_scoped_devices(self):
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            agent_id = await self._agent_with_skill(c, site_id)
            result = await _preflight(c, agent_id)
            assert result["skills"][0]["usable"] is True

            res = await c.post(f"/api/operational-agents/{agent_id}/activate")
            assert res.status_code == 200, res.text
            assert res.json()["skill_installs"]["installed"] == 1

        assert len(FakeSM.installs) == 1, "activation did not install the skill"
        call = FakeSM.installs[0]
        assert call["skill_name"] == "fan-health"
        # A19.11: named devices only, never the whole site.
        assert call["device_agent_ids"] == ["node-1"]
        assert call["issued_by"] == f"op-agent:{agent_id}@v1"

    @pytest.mark.asyncio
    async def test_reactivation_does_not_install_the_same_skill_twice(self):
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            agent_id = await self._agent_with_skill(c, site_id)
            await _preflight(c, agent_id)
            await c.post(f"/api/operational-agents/{agent_id}/activate")
            assert len(FakeSM.installs) == 1

            await c.post(f"/api/operational-agents/{agent_id}/pause")
            await _preflight(c, agent_id)
            res = await c.post(f"/api/operational-agents/{agent_id}/activate")
            assert res.status_code == 200, res.text

        assert len(FakeSM.installs) == 1, "re-activation installed it again"

    @pytest.mark.asyncio
    async def test_the_installation_is_recorded_and_audited(self):
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            agent_id = await self._agent_with_skill(c, site_id)
            await _preflight(c, agent_id)
            await c.post(f"/api/operational-agents/{agent_id}/activate")

            runtime = (
                await c.get(f"/api/operational-agents/{agent_id}/runtime")
            ).json()
            assert runtime["skills"] == {"queued": 1}

            entries = (await c.get("/api/audit/?limit=100")).json()["entries"]
            actions = {e["action"] for e in entries}
            assert "operational_agent.skills_installed" in actions
            assert (await c.get("/api/audit/verify")).json()["valid"] is True

    @pytest.mark.asyncio
    async def test_an_agent_with_no_skills_installs_nothing(self):
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            res = await c.post("/api/operational-agents/", json=_body(site_id))
            agent_id = res.json()["id"]
            await _preflight(c, agent_id)
            await c.post(f"/api/operational-agents/{agent_id}/activate")
        assert FakeSM.installs == []

    @pytest.mark.asyncio
    async def test_an_unusable_skill_is_not_installed(self):
        """A skill recommending what no executor implements is refused.

        Reported before activation rather than discovered at dispatch --
        and never delivered to a device that cannot run it.
        """
        stack = await _stack()
        site_id = await _seed(stack)
        # CLEAR_COUNTERS is governed vocabulary with zero executor reach
        # anywhere in the platform (A17.6) -- exactly the case the
        # Registry exists to catch before anything is dispatched.
        TestSkillInstallOnActivation.recommends = ("CLEAR_COUNTERS",)

        async with stack.client() as c:
            agent_id = await self._agent_with_skill(c, site_id)
            result = await _preflight(c, agent_id)
            assert result["skills"][0]["usable"] is False
            assert "CLEAR_COUNTERS" in result["skills"][0]["unsupported"]
            await c.post(f"/api/operational-agents/{agent_id}/activate")

        assert FakeSM.installs == [], "an unusable skill was installed anyway"
