"""E0.1: the approval policy actually binds, on both origins.

The defect this closes: a tenant could configure dual authorization and
get single authorization, silently. These drive the wired endpoints over
HTTP, because the only thing that proves server-side enforcement is a
request that is refused.
"""

from __future__ import annotations

import httpx
import pytest

from harkeniq_cc.api import approvals as approvals_api
from harkeniq_cc.api.deps import get_current_user
from harkeniq_cc.app import create_app
from harkeniq_cc.auth import ROLE_PERMISSIONS, UserContext, configure_auth
from harkeniq_cc.config import CCConfig
from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_cc.db.models import CCApprovalRoute, CCFleetCache, CCSite
from harkeniq_cc.runtime import AppState

TENANT = "t1"


class FakeSM:
    """Records what was routed; never touches a network."""

    calls: list = []

    def __init__(self, *_a, **_kw):
        pass

    async def route_approval(self, **kw):
        FakeSM.calls.append(kw)
        return {"accepted": True, "delivered": True, "reason": ""}

    async def dispatch_action(self, endpoint, token, **kw):
        FakeSM.calls.append(kw)
        return {"accepted": True, "directive_id": "dir-1", "reason": ""}


@pytest.fixture(autouse=True)
def _fake_sm(monkeypatch):
    FakeSM.calls = []
    monkeypatch.setattr(approvals_api, "SMClient", FakeSM)
    yield


class Stack:
    """One CC app that can answer as several different people."""

    def __init__(self, app, sessionmaker):
        self.app = app
        self.sessionmaker = sessionmaker
        self.persona = ("kc-owner", "owner@example.com", "tenant_owner")

    def as_person(self, sub, email, role="operator"):
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
    stack = Stack(app, sessionmaker)

    async def _fake():
        sub, email, role = stack.persona
        return UserContext(
            user_id=sub, email=email, tenant_id=TENANT, role=role,
            permissions=list(ROLE_PERMISSIONS[role]),
        )

    app.dependency_overrides[get_current_user] = _fake
    return stack


async def _seed(stack: Stack, *, action_id="act-1", action_type="POWER_CYCLE") -> str:
    async with stack.sessionmaker() as session:
        site = CCSite(
            tenant_id=TENANT, site_name="DC-1",
            sm_endpoint="sm:50051", sm_token="tok",
        )
        session.add(site)
        await session.flush()
        session.add(CCFleetCache(
            site_id=site.id, agent_id="node-1", agent_name="n1",
            vendor="Dell", model="R750", device_class="server",
            observation="observed", health="OK",
        ))
        session.add(CCApprovalRoute(
            site_id=site.id, action_id=action_id, action_type=action_type,
            device_agent_id="node-1",
        ))
        await session.commit()
        return site.id


async def _policy(client, **kw):
    body = {"name": "dual", "action_type": "*", "device_type": "*",
            "risk_level": "*", "required_approvers": 2}
    body.update(kw)
    res = await client.post("/api/policies/", json=body)
    assert res.status_code == 200, res.text
    return res.json()["policy"]


class TestDualApprovalNodeOrigin:
    @pytest.mark.asyncio
    async def test_one_approval_records_and_does_not_execute(self):
        stack = await _stack()
        await _seed(stack)
        async with stack.client() as c:
            await _policy(c)
            stack.as_person("kc-a", "a@example.com")
            res = await c.post("/api/approvals/act-1/approve")
            assert res.status_code == 200
            body = res.json()
            assert body["decision"] is None
            assert body["recorded"] is True
            assert body["approval"]["required"] == 2
            assert body["approval"]["received"] == 1
            assert body["approval"]["remaining"] == 1
            # Nothing was routed to the site.
            assert FakeSM.calls == []

    @pytest.mark.asyncio
    async def test_the_second_approver_completes_it(self):
        stack = await _stack()
        await _seed(stack)
        async with stack.client() as c:
            await _policy(c)
            stack.as_person("kc-a", "a@example.com")
            await c.post("/api/approvals/act-1/approve")
            stack.as_person("kc-b", "b@example.com")
            res = await c.post("/api/approvals/act-1/approve")
            assert res.status_code == 200
            body = res.json()
            assert body["decision"] == "approved"
            assert body["approval"]["received"] == 2
            assert body["delivery"]["delivered"] is True
            assert len(FakeSM.calls) == 1

    @pytest.mark.asyncio
    async def test_the_same_approver_cannot_approve_twice(self):
        stack = await _stack()
        await _seed(stack)
        async with stack.client() as c:
            await _policy(c)
            stack.as_person("kc-a", "a@example.com")
            assert (await c.post("/api/approvals/act-1/approve")).status_code == 200
            res = await c.post("/api/approvals/act-1/approve")
            assert res.status_code == 409
            assert "already approved" in res.json()["detail"]
            assert FakeSM.calls == []

    @pytest.mark.asyncio
    async def test_one_denial_is_terminal_under_a_dual_policy(self):
        stack = await _stack()
        await _seed(stack)
        async with stack.client() as c:
            await _policy(c)
            stack.as_person("kc-a", "a@example.com")
            await c.post("/api/approvals/act-1/approve")
            stack.as_person("kc-b", "b@example.com")
            res = await c.post("/api/approvals/act-1/deny")
            assert res.json()["decision"] == "denied"
            # Routed as a denial, not silently dropped.
            assert FakeSM.calls[0]["decision"] == "denied"

    @pytest.mark.asyncio
    async def test_without_a_policy_one_approval_still_decides(self):
        """Behaviour every tenant has today must be unchanged."""
        stack = await _stack()
        await _seed(stack)
        async with stack.client() as c:
            stack.as_person("kc-a", "a@example.com")
            res = await c.post("/api/approvals/act-1/approve")
            assert res.json()["decision"] == "approved"
            assert res.json()["approval"]["required"] == 1
            assert len(FakeSM.calls) == 1


class TestPolicySpecificity:
    @pytest.mark.asyncio
    async def test_the_action_specific_policy_governs(self):
        stack = await _stack()
        await _seed(stack, action_type="POWER_CYCLE")
        async with stack.client() as c:
            await _policy(c, name="broad", required_approvers=1)
            await _policy(
                c, name="power-dual", action_type="POWER_CYCLE",
                required_approvers=2,
            )
            stack.as_person("kc-a", "a@example.com")
            body = (await c.post("/api/approvals/act-1/approve")).json()
            assert body["approval"]["required"] == 2
            assert body["approval"]["policy_name"] == "power-dual"


class TestGroupMembership:
    @pytest.mark.asyncio
    async def test_a_non_member_is_refused(self):
        stack = await _stack()
        await _seed(stack)
        async with stack.client() as c:
            group = (await c.post("/api/policies/groups", json={
                "name": "SRE on-call", "required_count": 1,
            })).json()["group"]
            await c.post(f"/api/policies/groups/{group['id']}/members", json={
                "email": "a@example.com", "principal_ref": "kc-a",
            })
            await _policy(c, required_approvers=1, group_id=group["id"])

            stack.as_person("kc-z", "z@example.com")
            res = await c.post("/api/approvals/act-1/approve")
            assert res.status_code == 403
            assert "SRE on-call" in res.json()["detail"]
            assert FakeSM.calls == []

    @pytest.mark.asyncio
    async def test_a_member_may_approve(self):
        stack = await _stack()
        await _seed(stack)
        async with stack.client() as c:
            group = (await c.post("/api/policies/groups", json={
                "name": "SRE on-call", "required_count": 1,
            })).json()["group"]
            await c.post(f"/api/policies/groups/{group['id']}/members", json={
                "email": "a@example.com", "principal_ref": "kc-a",
            })
            await _policy(c, required_approvers=1, group_id=group["id"])
            stack.as_person("kc-a", "a@example.com")
            assert (
                await c.post("/api/approvals/act-1/approve")
            ).json()["decision"] == "approved"

    @pytest.mark.asyncio
    async def test_membership_survives_an_email_change(self):
        """Matched on the Keycloak subject, so a rename does not silently
        lapse someone's approval authority."""
        stack = await _stack()
        await _seed(stack)
        async with stack.client() as c:
            group = (await c.post("/api/policies/groups", json={
                "name": "SRE", "required_count": 1,
            })).json()["group"]
            await c.post(f"/api/policies/groups/{group['id']}/members", json={
                "email": "old@example.com", "principal_ref": "kc-a",
            })
            await _policy(c, required_approvers=1, group_id=group["id"])
            stack.as_person("kc-a", "new@example.com")
            assert (
                await c.post("/api/approvals/act-1/approve")
            ).json()["decision"] == "approved"


class TestAutoApproveRefused:
    @pytest.mark.asyncio
    async def test_creating_an_auto_approve_policy_is_refused(self):
        """Unattended execution is granted by the autonomy contract, which
        requires evidence and a human, not by an approval policy."""
        stack = await _stack()
        await _seed(stack)
        async with stack.client() as c:
            res = await c.post("/api/policies/", json={
                "name": "auto", "approval_mode": "auto_approve",
            })
            assert res.status_code == 400
            assert "autonomy contract" in res.json()["detail"]

    @pytest.mark.asyncio
    async def test_updating_a_policy_to_auto_approve_is_refused(self):
        stack = await _stack()
        await _seed(stack)
        async with stack.client() as c:
            policy = await _policy(c)
            res = await c.patch(
                f"/api/policies/{policy['id']}",
                json={"approval_mode": "auto_approve"},
            )
            assert res.status_code == 400


class TestQueueAndEvidence:
    @pytest.mark.asyncio
    async def test_the_queue_shows_progress_so_a_second_approver_knows(self):
        stack = await _stack()
        await _seed(stack)
        async with stack.client() as c:
            await _policy(c)
            stack.as_person("kc-a", "a@example.com")
            await c.post("/api/approvals/act-1/approve")
            queue = (await c.get("/api/approvals/")).json()
            item = [a for a in queue["actions"] if a["action_id"] == "act-1"][0]
            assert item["approval"]["received"] == 1
            assert item["approval"]["remaining"] == 1

    @pytest.mark.asyncio
    async def test_every_approval_is_individually_readable(self):
        stack = await _stack()
        await _seed(stack)
        async with stack.client() as c:
            await _policy(c)
            stack.as_person("kc-a", "a@example.com")
            await c.post("/api/approvals/act-1/approve")
            stack.as_person("kc-b", "b@example.com")
            await c.post("/api/approvals/act-1/approve")
            records = (await c.get("/api/approvals/act-1/records")).json()
            assert records["total"] == 2
            assert {r["approver"] for r in records["records"]} == {
                "a@example.com", "b@example.com",
            }

    @pytest.mark.asyncio
    async def test_each_approval_is_audited_not_only_the_outcome(self):
        stack = await _stack()
        await _seed(stack)
        async with stack.client() as c:
            await _policy(c)
            stack.as_person("kc-a", "a@example.com")
            await c.post("/api/approvals/act-1/approve")
            stack.as_person("kc-b", "b@example.com")
            await c.post("/api/approvals/act-1/approve")
            stack.as_person("kc-owner", "owner@example.com", "tenant_owner")
            entries = (await c.get("/api/audit/")).json()["entries"]
            approvals = [e for e in entries if e["action"] == "approval.approved"]
            assert len(approvals) == 2, "one audit entry per approver"
            assert (await c.get("/api/audit/verify")).json()["valid"] is True


class TestGovernance:
    @pytest.mark.asyncio
    async def test_a_viewer_cannot_decide(self):
        stack = await _stack()
        await _seed(stack)
        async with stack.client() as c:
            stack.as_person("kc-v", "v@example.com", "viewer")
            assert (
                await c.post("/api/approvals/act-1/approve")
            ).status_code == 403

    @pytest.mark.asyncio
    async def test_an_auditor_cannot_decide(self):
        stack = await _stack()
        await _seed(stack)
        async with stack.client() as c:
            stack.as_person("kc-au", "au@example.com", "auditor")
            assert (
                await c.post("/api/approvals/act-1/approve")
            ).status_code == 403

    @pytest.mark.asyncio
    async def test_an_operator_cannot_write_policy(self):
        """Deciding is not configuring."""
        stack = await _stack()
        await _seed(stack)
        async with stack.client() as c:
            stack.as_person("kc-op", "op@example.com", "operator")
            res = await c.post("/api/policies/", json={"name": "x"})
            assert res.status_code == 403


class TestAgentOriginSharesTheContract:
    """An agent's request earns no easier path to a decision."""

    async def _proposal(self, stack, site_id, action_type="POWER_CYCLE"):
        """A proposal, and the ACTIVE agent it is attributed to.

        A2's dispatch gate re-checks the proposing agent at decision time
        (D3): identity, activation state, tenant and safety, against the
        world as it is now rather than as it was when the proposal was
        made. A proposal attributed to an agent that does not exist is
        correctly refused, so this fixture builds the agent it names.

        The agent row is created directly rather than through the API
        because this file tests the APPROVAL ledger, not the activation
        lifecycle -- but its state is the state a real activation
        produces: active, at the version the attribution key names.
        """
        from harkeniq_cc.db.models import CCAgentProposal, CCOperationalAgent

        async with stack.sessionmaker() as session:
            agent = CCOperationalAgent(
                id="ag1", tenant_id=TENANT, name="fixture-agent",
                description="the agent this proposal is attributed to",
                version=1, status="active", activated_version=1,
                autonomy_ceiling=0, require_approval_always=True,
                created_by="fixture", updated_by="fixture",
            )
            session.add(agent)
            row = CCAgentProposal(
                tenant_id=TENANT, agent_id="ag1", actor="op-agent:ag1@v1",
                agent_version=1, site_id=site_id, device_agent_id="node-1",
                action_type=action_type, params={}, rationale="because",
                evidence={}, disposition="requires_approval",
                authorization_basis="human_approval",
                status="awaiting_approval",
                dedupe_key="ag1:node-1:POWER_CYCLE:i1",
            )
            session.add(row)
            await session.commit()
            return row.id

    @pytest.mark.asyncio
    async def test_a_dual_policy_binds_an_agent_proposal_too(self):
        stack = await _stack()
        site_id = await _seed(stack)
        proposal_id = await self._proposal(stack, site_id)
        async with stack.client() as c:
            await _policy(c)
            stack.as_person("kc-a", "a@example.com")
            first = await c.post(f"/api/approvals/{proposal_id}/approve")
            assert first.json()["recorded"] is True
            assert first.json()["approval"]["remaining"] == 1
            assert FakeSM.calls == [], "nothing dispatched on one approval"

            stack.as_person("kc-b", "b@example.com")
            second = await c.post(f"/api/approvals/{proposal_id}/approve")
            assert second.json()["decision"] == "approved"
            assert second.json()["origin"] == "agent"
            assert second.json()["delivery"]["delivered"] is True
            assert len(FakeSM.calls) == 1

    @pytest.mark.asyncio
    async def test_duplicate_approver_refused_on_the_agent_path(self):
        stack = await _stack()
        site_id = await _seed(stack)
        proposal_id = await self._proposal(stack, site_id)
        async with stack.client() as c:
            await _policy(c)
            stack.as_person("kc-a", "a@example.com")
            await c.post(f"/api/approvals/{proposal_id}/approve")
            res = await c.post(f"/api/approvals/{proposal_id}/approve")
            assert res.status_code == 409

    @pytest.mark.asyncio
    async def test_agent_proposal_approvals_are_individually_readable(self):
        stack = await _stack()
        site_id = await _seed(stack)
        proposal_id = await self._proposal(stack, site_id)
        async with stack.client() as c:
            await _policy(c)
            stack.as_person("kc-a", "a@example.com")
            await c.post(f"/api/approvals/{proposal_id}/approve")
            records = (await c.get(f"/api/approvals/{proposal_id}/records")).json()
            assert records["subject_type"] == "agent_proposal"
            assert records["total"] == 1


class TestPolicySelectorDefaults:
    """Found on the live stack: a policy created as 'dual approval for
    everything' governed medium-risk actions only, silently, because
    risk_level defaulted to 'medium' while the other two selectors
    defaulted to the wildcard."""

    @pytest.mark.asyncio
    async def test_a_policy_created_with_defaults_governs_every_risk_band(self):
        stack = await _stack()
        # COLLECT_DIAGNOSTICS is risk "none" -- a policy defaulting to
        # "medium" would not have matched it.
        await _seed(stack, action_type="COLLECT_DIAGNOSTICS")
        async with stack.client() as c:
            res = await c.post(
                "/api/policies/", json={"name": "everything", "required_approvers": 2},
            )
            assert res.json()["policy"]["risk_level"] == "*"
            stack.as_person("kc-a", "a@example.com")
            body = (await c.post("/api/approvals/act-1/approve")).json()
            assert body["approval"]["required"] == 2, (
                "a wildcard policy must govern a risk-none action"
            )

    @pytest.mark.asyncio
    async def test_a_risk_scoped_policy_still_only_governs_that_band(self):
        stack = await _stack()
        await _seed(stack, action_type="COLLECT_DIAGNOSTICS")
        async with stack.client() as c:
            await _policy(c, name="medium-only", risk_level="medium",
                          required_approvers=2)
            stack.as_person("kc-a", "a@example.com")
            body = (await c.post("/api/approvals/act-1/approve")).json()
            assert body["decision"] == "approved"
            assert body["approval"]["required"] == 1


class TestQueueAgreesWithTheDecision:
    @pytest.mark.asyncio
    async def test_a_device_scoped_policy_shows_the_same_count_it_enforces(self):
        """The queue must resolve the policy the decision will resolve, or
        it promises one approver and then demands two."""
        stack = await _stack()
        await _seed(stack, action_type="COLLECT_DIAGNOSTICS")
        async with stack.client() as c:
            await _policy(
                c, name="servers-dual", device_type="server", required_approvers=2,
            )
            listed = (await c.get("/api/approvals/")).json()["actions"][0]
            assert listed["approval"]["required"] == 2

            stack.as_person("kc-a", "a@example.com")
            decided = (await c.post("/api/approvals/act-1/approve")).json()
            assert decided["approval"]["required"] == listed["approval"]["required"]
            assert decided.get("decision") is None
