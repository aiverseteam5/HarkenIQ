"""A1: the governed agent loop, end to end inside Central Command.

Create a named agent, scope it, bind it, activate it, let it observe a
real incident, and follow the proposal through the ONE approval queue, a
human decision, dispatch to the site that owns the device, and back as
an attributed outcome.

What these tests are really pinning: an agent's work uses the same
queue, the same permission and the same audit chain a human's does, and
it can never dispatch anything the tenant's own contract has not already
allowed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from harkeniq_cc import agent_runtime
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
from harkeniq_cc.db.repos import AgentProposalRepo
from harkeniq_cc.runtime import AppState

TENANT = "t1"


class FakeSM:
    """Records dispatches; never touches a network."""

    def __init__(self, *_a, **_kw):
        FakeSM.calls = getattr(FakeSM, "calls", [])
        self.accepted = FakeSM.accepted

    accepted = True
    reason = ""
    calls: list = []

    async def dispatch_action(self, endpoint, token, **kw):
        FakeSM.calls.append(kw)
        if not FakeSM.accepted:
            return {"accepted": False, "directive_id": "", "reason": FakeSM.reason}
        return {
            "accepted": True,
            "directive_id": f"dir-{len(FakeSM.calls)}",
            "reason": "",
        }


@pytest.fixture(autouse=True)
def _fake_sm(monkeypatch):
    FakeSM.calls = []
    FakeSM.accepted = True
    FakeSM.reason = ""
    monkeypatch.setattr(agent_runtime, "SMClient", FakeSM)
    monkeypatch.setattr(approvals_api, "SMClient", FakeSM)
    yield


async def _stack(role: str = "tenant_owner"):
    config = CCConfig(tenant_id=TENANT, insecure=True)
    configure_auth("", "", "", insecure=True)
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    sessionmaker = make_sessionmaker(engine)
    state = AppState(config=config, engine=engine, sessionmaker=sessionmaker)
    app = create_app(state)

    async def _fake():
        return UserContext(
            user_id=f"kc-{role}", email=f"{role}@example.com", tenant_id=TENANT,
            role=role, permissions=list(ROLE_PERMISSIONS[role]),
        )

    app.dependency_overrides[get_current_user] = _fake
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    )
    return client, state


async def _seed(state, *, level: int = 2, incident: bool = True) -> str:
    async with state.sessionmaker() as session:
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
        ))
        session.add(CCAutonomyBudget(
            tenant_id=TENANT, device_type="*", level=level,
            budget_limit=10, budget_period="daily",
        ))
        if incident:
            session.add(CCIncident(
                incident_id="inc-1", tenant_id=TENANT, site_id=site.id,
                kind="device", status="open", title="BMC event log saturated",
                device_agent_id="node-1", subsystem="log", confidence=0.9,
            ))
        await session.commit()
        return site.id


async def _make_agent(client, site_id, **overrides):
    body = {
        "name": "Night Shift",
        "scopes": [{"scope_type": "site", "scope_ref": site_id}],
        "capabilities": [{"kind": "action_class", "capability_ref": "SEL_CLEAR"}],
    }
    body.update(overrides)
    created = await client.post("/api/operational-agents/", json=body)
    assert created.status_code == 201, created.text
    agent_id = created.json()["id"]
    # A2: activation is gated on a stored preflight for this exact
    # configuration version, so the journey now runs through it. The
    # fixture's devices have not declared capabilities, which reads as
    # UNKNOWN rather than incapable, so the warnings are acknowledged --
    # by a named human, exactly as an operator would.
    pre = await client.post(f"/api/operational-agents/{agent_id}/preflight")
    assert pre.status_code == 200, pre.text
    result = pre.json()
    if result.get("requires_acknowledgement"):
        ack = await client.post(f"/api/operational-agents/{agent_id}/acknowledge")
        assert ack.status_code == 200, ack.text
    # A2 D1: an agent whose configuration grants UNATTENDED execution
    # needs a named human to approve activating it -- on the same
    # approvals queue a node action uses. A propose-only agent needs
    # none, which is why this is conditional rather than a ceremony.
    if result.get("requires_activation_approval"):
        detail = await client.get(f"/api/operational-agents/{agent_id}")
        subject = detail.json()["agent"].get("activation_subject_ref")
        assert subject, "an activation needing approval must name its subject"
        approved = await client.post(f"/api/approvals/{subject}/approve")
        assert approved.status_code == 200, approved.text
    activated = await client.post(f"/api/operational-agents/{agent_id}/activate")
    assert activated.status_code == 200, activated.text
    return agent_id


class TestTheGovernedJourney:
    @pytest.mark.asyncio
    async def test_propose_approve_dispatch_settle(self):
        client, state = await _stack()
        site_id = await _seed(state)
        agent_id = await _make_agent(client, site_id)

        # 1. The agent observes and proposes.
        stats = await agent_runtime.run_once(state, TENANT)
        assert stats["proposed"] == 1
        assert stats["awaiting_approval"] == 1
        assert stats["dispatched"] == 0  # nothing decided yet

        # 2. It lands in the ONE approvals queue, with its evidence.
        queue = (await client.get("/api/approvals/")).json()
        assert queue["agent_total"] == 1
        item = queue["actions"][0]
        assert item["origin"] == "agent"
        assert item["action_type"] == "SEL_CLEAR"
        assert item["proposal"]["actor"].startswith("op-agent:")
        assert "BMC event log saturated" in item["proposal"]["rationale"]
        assert item["proposal"]["evidence"]["incident_ids"] == ["inc-1"]

        # 3. A named human decides it, on the same endpoint as any action.
        decided = await client.post(f"/api/approvals/{item['action_id']}/approve")
        assert decided.status_code == 200
        body = decided.json()
        assert body["origin"] == "agent"
        assert body["decided_by"] == "tenant_owner@example.com"
        assert body["delivery"]["delivered"] is True

        # 4. Dispatch carried the attribution and the basis.
        assert len(FakeSM.calls) == 1
        call = FakeSM.calls[0]
        assert call["actor"].startswith("op-agent:")
        assert call["authorization"] == "human_approval"
        assert call["decided_by"] == "tenant_owner@example.com"
        assert call["device_agent_id"] == "node-1"
        assert call["action_type"] == "SEL_CLEAR"

        # 5. The outcome comes back attributed and settles the proposal.
        async with state.sessionmaker() as session:
            proposal = (await AgentProposalRepo(session).list_for_agent(
                TENANT, agent_id
            ))[0]
            session.add(CCOutcomeHistory(
                site_id=site_id, action_id="act-1", action_type="SEL_CLEAR",
                device_agent_id="node-1", outcome="SUCCESS",
                fault_resolved=True, actor=proposal.actor,
                ingested_at=datetime.now(timezone.utc) + timedelta(seconds=1),
            ))
            await session.commit()
        assert await agent_runtime.settle_outcomes(state, TENANT) == 1

        view = (await client.get(f"/api/operational-agents/{agent_id}")).json()
        assert view["activity"]["executed"] == 1
        assert view["activity"]["succeeded"] == 1
        assert view["proposals"][0]["status"] == "completed"

        # 6. The whole chain is on the existing audit chain, actor-labelled.
        entries = (await client.get("/api/audit/")).json()["entries"]
        actions = {e["action"] for e in entries}
        assert {
            "operational_agent.created", "operational_agent.activated",
            "agent_proposal.created", "action.approved",
            "agent_proposal.settled",
        } <= actions
        agent_entries = [
            e for e in entries if e["actor"].startswith("op-agent:")
        ]
        assert agent_entries, "the agent must be named in the audit chain"
        await client.aclose()

    @pytest.mark.asyncio
    async def test_autonomous_path_dispatches_without_a_human(self):
        client, state = await _stack()
        site_id = await _seed(state, level=2)
        await _make_agent(
            client, site_id,
            require_approval_always=False, autonomy_ceiling=2,
        )
        stats = await agent_runtime.run_once(state, TENANT)
        assert stats["autonomous"] == 1
        assert stats["dispatched"] == 1
        call = FakeSM.calls[0]
        assert call["authorization"] == "autonomous_grant"
        assert call["decided_by"].startswith("autonomy:")
        # No human decided it, so no human is named as the decider.
        queue = (await client.get("/api/approvals/")).json()
        assert queue["agent_total"] == 0
        await client.aclose()

    @pytest.mark.asyncio
    async def test_denial_is_final_and_never_dispatches(self):
        client, state = await _stack()
        site_id = await _seed(state)
        await _make_agent(client, site_id)
        await agent_runtime.run_once(state, TENANT)
        item = (await client.get("/api/approvals/")).json()["actions"][0]
        denied = await client.post(f"/api/approvals/{item['action_id']}/deny")
        assert denied.status_code == 200
        assert FakeSM.calls == []
        # A second decision on the same proposal is refused.
        again = await client.post(f"/api/approvals/{item['action_id']}/approve")
        assert again.status_code == 409
        # And the agent does not re-propose the same work.
        assert (await agent_runtime.run_once(state, TENANT))["proposed"] == 0
        await client.aclose()

    @pytest.mark.asyncio
    async def test_a_refusing_site_records_why_and_keeps_the_decision(self):
        client, state = await _stack()
        site_id = await _seed(state)
        agent_id = await _make_agent(client, site_id)
        await agent_runtime.run_once(state, TENANT)
        FakeSM.accepted = False
        FakeSM.reason = "site stop switch is active"
        item = (await client.get("/api/approvals/")).json()["actions"][0]
        res = await client.post(f"/api/approvals/{item['action_id']}/approve")
        assert res.json()["delivery"]["delivered"] is False
        proposals = (await client.get(
            f"/api/operational-agents/{agent_id}/proposals"
        )).json()["proposals"]
        assert proposals[0]["status"] == "failed"
        assert "stop switch" in proposals[0]["dispatch_reason"]
        # The human's decision is still recorded: they did decide.
        assert proposals[0]["decided_by"] == "tenant_owner@example.com"
        await client.aclose()


class TestTheGovernanceHolds:
    @pytest.mark.asyncio
    async def test_a_settled_proposal_is_not_re_proposed_for_the_same_fault(self):
        """Found on the live stack: a permanently-refused action came
        back on every pass once its first attempt settled."""
        client, state = await _stack()
        site_id = await _seed(state, incident=False)
        agent_id = await _make_agent(
            client, site_id,
            require_approval_always=False, autonomy_ceiling=2,
            capabilities=[
                {"kind": "action_class", "capability_ref": "SEL_CLEAR"}
            ],
        )
        async with state.sessionmaker() as session:
            session.add(CCIncident(
                incident_id="inc-log", tenant_id=TENANT, site_id=site_id,
                kind="device", status="open", title="event log saturated",
                device_agent_id="node-1", subsystem="log",
            ))
            await session.commit()
        assert (await agent_runtime.run_once(state, TENANT))["proposed"] == 1
        # The node refused it; the proposal settles as failed.
        async with state.sessionmaker() as session:
            repo = AgentProposalRepo(session)
            proposal = (await repo.list_for_agent(TENANT, agent_id))[0]
            await repo.mark_failed(proposal, "not in allow list")
            await session.commit()
        # Same incident, same class: no second proposal.
        assert (await agent_runtime.run_once(state, TENANT))["proposed"] == 0
        # A DIFFERENT incident is new work and is proposed.
        async with state.sessionmaker() as session:
            session.add(CCIncident(
                incident_id="inc-log-2", tenant_id=TENANT, site_id=site_id,
                kind="device", status="open", title="event log saturated again",
                device_agent_id="node-1", subsystem="log",
            ))
            await session.commit()
        assert (await agent_runtime.run_once(state, TENANT))["proposed"] == 1
        await client.aclose()

    @pytest.mark.asyncio
    async def test_a_paused_agent_evaluates_nothing(self):
        client, state = await _stack()
        site_id = await _seed(state)
        agent_id = await _make_agent(client, site_id)
        await client.post(f"/api/operational-agents/{agent_id}/pause")
        assert (await agent_runtime.run_once(state, TENANT))["proposed"] == 0
        await client.aclose()

    @pytest.mark.asyncio
    async def test_a_draft_agent_evaluates_nothing(self):
        client, state = await _stack()
        site_id = await _seed(state)
        await client.post("/api/operational-agents/", json={
            "name": "Draft",
            "scopes": [{"scope_type": "site", "scope_ref": site_id}],
            "capabilities": [
                {"kind": "action_class", "capability_ref": "SEL_CLEAR"}
            ],
        })
        assert (await agent_runtime.run_once(state, TENANT))["proposed"] == 0
        await client.aclose()

    @pytest.mark.asyncio
    async def test_a_fenced_class_is_blocked_and_never_dispatched(self):
        """FIRMWARE_UPDATE is never budget-grantable, at any level."""
        client, state = await _stack()
        site_id = await _seed(state, level=3)
        async with state.sessionmaker() as session:
            session.add(CCIncident(
                incident_id="inc-2", tenant_id=TENANT, site_id=site_id,
                kind="device", status="open", title="disk failing",
                device_agent_id="node-1", subsystem="disk",
            ))
            await session.commit()
        await _make_agent(
            client, site_id,
            require_approval_always=False, autonomy_ceiling=3,
            capabilities=[
                {"kind": "action_class", "capability_ref": "FIRMWARE_UPDATE"},
                {"kind": "action_class", "capability_ref": "IDENTIFY_LED"},
            ],
        )
        await agent_runtime.run_once(state, TENANT)
        queue = (await client.get("/api/approvals/")).json()
        # Nothing ran unattended. FIRMWARE_UPDATE is fenced at every
        # level, and IDENTIFY_LED is risk-none but mapped to no level, so
        # it can only ever reach a human -- never the autonomous path,
        # even for an agent whose ceiling is 3.
        assert FakeSM.calls == []
        item = queue["actions"][0]
        assert item["proposal"]["action_type"] == "IDENTIFY_LED"
        assert item["proposal"]["status"] == "awaiting_approval"
        assert item["proposal"]["authorization_basis"] == "human_approval"
        assert any(
            b["code"] == "not_budget_mapped"
            for b in item["proposal"]["blocking_conditions"]
        )
        await client.aclose()

    @pytest.mark.asyncio
    async def test_stop_switch_blocks_but_still_records_the_intent(self):
        client, state = await _stack()
        site_id = await _seed(state)
        agent_id = await _make_agent(
            client, site_id,
            require_approval_always=False, autonomy_ceiling=2,
        )
        await client.post("/api/policies/stop-switch", json={
            "active": True, "reason": "maintenance window",
        })
        stats = await agent_runtime.run_once(state, TENANT)
        assert stats["blocked"] == 1
        assert FakeSM.calls == []
        proposals = (await client.get(
            f"/api/operational-agents/{agent_id}/proposals"
        )).json()["proposals"]
        assert proposals[0]["status"] == "blocked"
        assert "stop switch" in proposals[0]["disposition_reason"]
        await client.aclose()

    @pytest.mark.asyncio
    async def test_node_originated_actions_still_appear_in_the_queue(self):
        """Unifying the queue must not evict the actions that were there."""
        client, state = await _stack()
        site_id = await _seed(state)
        await _make_agent(client, site_id)
        async with state.sessionmaker() as session:
            from harkeniq_cc.db.models import CCApprovalRoute

            session.add(CCApprovalRoute(
                site_id=site_id, action_id="act-node-1",
                action_type="COLLECT_DIAGNOSTICS", device_agent_id="node-1",
            ))
            await session.commit()
        await agent_runtime.run_once(state, TENANT)
        queue = (await client.get("/api/approvals/")).json()
        origins = {a["origin"] for a in queue["actions"]}
        assert origins == {"agent", "node"}
        assert queue["node_total"] == 1
        assert queue["agent_total"] == 1
        await client.aclose()


class TestTheDispatchIsAlwaysAudited:
    """A human approval dispatches synchronously in the approval path, so
    the background loop that normally writes `agent_proposal.dispatched`
    never sees the proposal. Without the event the audit chain reads
    created -> settled with the dispatch invisible, and "the whole chain
    is reconstructable" stops being true for every human-approved agent
    action. Found by the compose gate."""

    def test_the_approval_path_writes_the_dispatch_event(self):
        import inspect

        from harkeniq_cc.api import approvals

        source = inspect.getsource(approvals._decide_agent_proposal)
        assert 'action="agent_proposal.dispatched"' in source, (
            "the synchronous dispatch path does not audit the dispatch"
        )

    def test_both_dispatch_paths_write_the_same_event(self):
        import inspect

        from harkeniq_cc import agent_runtime
        from harkeniq_cc.api import approvals

        loop = inspect.getsource(agent_runtime.dispatch_decided)
        sync = inspect.getsource(approvals._decide_agent_proposal)
        for source in (loop, sync):
            assert 'action="agent_proposal.dispatched"' in source
