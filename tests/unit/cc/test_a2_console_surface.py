"""A2 completion: what the Console consumes, and the gap it exposed.

The Console must be able to answer twelve questions about an agent
without deriving anything in the browser. That means Central Command has
to SERVE the answers -- and building the page found one place where it
did not: an activation waiting on a human never appeared in the one
approval queue, so an approver could only reach it if somebody handed
them the subject digest from the agent page. A queue nobody can find an
item in is a second approval surface in everything but name.

These tests cover the composed reads the page renders, and they assert
the CALL: that the queue lists activations, that the readiness read
reports whether its own approval is satisfied, and that per-device skill
delivery is visible rather than summarised into a number.
"""

from __future__ import annotations

import httpx
import pytest

from harkeniq_cc import skill_fetch, sm_client
from harkeniq_cc.api import approvals as approvals_api
from harkeniq_cc.api.deps import get_current_user
from harkeniq_cc.app import create_app
from harkeniq_cc.auth import ROLE_PERMISSIONS, UserContext, configure_auth
from harkeniq_cc.config import CCConfig
from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_cc.db.models import CCAutonomyBudget, CCFleetCache, CCSite
from harkeniq_cc.runtime import AppState

from tests.unit.cc.conftest import seed_tenant_people

TENANT = "t1"


class FakeSM:
    installs: list = []

    def __init__(self, *_a, **_kw):
        pass

    async def dispatch_action(self, endpoint, token, **kw):
        return {"accepted": True, "directive_id": "dir-1", "reason": ""}

    async def route_approval(self, **kw):
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
    def __init__(self, name, recommends=("SEL_CLEAR",)):
        self.name = name
        self.version = "1"
        self.raw_yaml = f"name: {name}\n"
        self.rules = [_FakeRule(a) for a in recommends]


@pytest.fixture(autouse=True)
def _fakes(monkeypatch):
    FakeSM.installs = []
    monkeypatch.setattr(approvals_api, "SMClient", FakeSM)
    monkeypatch.setattr(sm_client, "SMClient", FakeSM)

    async def _fetch(state, tenant_id, skill_id):
        return FakeSkill(skill_id), ""

    monkeypatch.setattr(skill_fetch, "fetch_skill_definition", _fetch)
    yield


class Stack:
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

    # A23-5: a rowless tenant is STRICT now (A23.11). The default
    # persona is the tenant's founding administrator, granted the
    # way tenant birth grants one (A23.14 D4) rather than being
    # tenant-wide by the synthesis a missing row used to give.
    await seed_tenant_people(sessionmaker, TENANT, [
        ("kc-owner", "tenant_owner"),
        ("kc-a", "operator"), ("kc-b", "operator"), ("kc-z", "operator"),
        ("kc-op", "operator"), ("kc-au", "auditor"), ("kc-v", "viewer"),
    ])

    async def _fake():
        sub, email, role = stack.persona
        return UserContext(
            user_id=sub, email=email, tenant_id=TENANT, role=role,
            permissions=list(ROLE_PERMISSIONS[role]),
        )

    app.dependency_overrides[get_current_user] = _fake
    return stack


async def _seed(stack: Stack, *, level: int = 2) -> str:
    async with stack.sessionmaker() as session:
        site = CCSite(tenant_id=TENANT, site_name="DC-1",
                      sm_endpoint="sm:50051", sm_token="tok")
        session.add(site)
        await session.flush()
        session.add(CCFleetCache(
            site_id=site.id, agent_id="node-1", agent_name="rack1-node1",
            vendor="Dell", model="R750", device_class="server",
            observation="observed", health="OK",
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


async def _unattended_agent(c, site_id, **kw):
    body = _body(site_id, autonomy_ceiling=2, require_approval_always=False)
    body.update(kw)
    res = await c.post("/api/operational-agents/", json=body)
    assert res.status_code == 201, res.text
    return res.json()["id"]


async def _preflight(c, agent_id) -> dict:
    """PREFLIGHT, then ACKNOWLEDGE where the contract asks for it.

    These fixtures have no site reporting live safety state, so `safety`
    reads UNKNOWN -- correctly: suppressions and error budgets are not
    assumed clear just because nothing said otherwise. That makes the
    acknowledgement step real here, which is the workflow an operator
    walks, so the tests walk it too.
    """
    res = await c.post(f"/api/operational-agents/{agent_id}/preflight")
    assert res.status_code == 200, res.text
    result = res.json()
    if result.get("requires_acknowledgement"):
        ack = await c.post(f"/api/operational-agents/{agent_id}/acknowledge")
        assert ack.status_code == 200, ack.text
    return result


# ---------------------------------------------------------------------------
# The gap building the page exposed
# ---------------------------------------------------------------------------


class TestActivationsAppearInTheOneQueue:
    """An approval nobody can find is not on the queue in any real sense."""

    @pytest.mark.asyncio
    async def test_a_pending_activation_is_listed_with_the_other_origins(self):
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            agent_id = await _unattended_agent(c, site_id)
            result = await _preflight(c, agent_id)
            assert result["requires_activation_approval"] is True

            queue = (await c.get("/api/approvals/")).json()
            assert queue["activation_total"] == 1, queue
            item = next(
                i for i in queue["actions"] if i["origin"] == "agent_activation"
            )
            # The SAME envelope a node action and an agent proposal carry.
            for field in ("origin", "id", "action_id", "action_type",
                          "device_agent_id", "decision", "routed_at"):
                assert field in item, field
            # And the context a human needs to decide THIS.
            act = item["activation"]
            assert act["agent_id"] == agent_id
            assert "SEL_CLEAR" in act["unattended_classes"]
            assert act["configuration_version"] == 1
            assert "does not activate" in act["note"]

    @pytest.mark.asyncio
    async def test_the_queue_item_decides_on_the_normal_endpoint(self):
        """No agent-only decision path: the same POST, the same ledger."""
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            agent_id = await _unattended_agent(c, site_id)
            await _preflight(c, agent_id)
            item = next(
                i for i in (await c.get("/api/approvals/")).json()["actions"]
                if i["origin"] == "agent_activation"
            )
            res = await c.post(f"/api/approvals/{item['action_id']}/approve")
            assert res.status_code == 200, res.text
            assert res.json()["origin"] == "agent_activation"

            # Decided, so it leaves the pending queue.
            after = (await c.get("/api/approvals/")).json()
            assert after["activation_total"] == 0

            assert (
                await c.post(f"/api/operational-agents/{agent_id}/activate")
            ).status_code == 200

    @pytest.mark.asyncio
    async def test_a_denied_activation_also_leaves_the_queue(self):
        """A denial is terminal (D16). It is not still awaiting a human."""
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            agent_id = await _unattended_agent(c, site_id)
            await _preflight(c, agent_id)
            subject = next(
                i for i in (await c.get("/api/approvals/")).json()["actions"]
                if i["origin"] == "agent_activation"
            )["action_id"]

            assert (
                await c.post(f"/api/approvals/{subject}/deny")
            ).status_code == 200
            assert (await c.get("/api/approvals/")).json()["activation_total"] == 0

            res = await c.post(f"/api/operational-agents/{agent_id}/activate")
            assert res.status_code == 409
            assert "denied" in res.json()["detail"]

    @pytest.mark.asyncio
    async def test_an_activated_agent_is_not_still_pending(self):
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            res = await c.post("/api/operational-agents/", json=_body(site_id))
            agent_id = res.json()["id"]
            result = await _preflight(c, agent_id)
            # Propose-only: D1 raises no subject at all.
            assert result["requires_activation_approval"] is False
            assert (await c.get("/api/approvals/")).json()["activation_total"] == 0

    @pytest.mark.asyncio
    async def test_a_dual_policy_keeps_it_in_the_queue_after_one_approval(self):
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            await c.post("/api/policies/", json={
                "name": "dual", "action_type": "*", "device_type": "*",
                "risk_level": "*", "required_approvers": 2,
            })
            agent_id = await _unattended_agent(c, site_id)
            await _preflight(c, agent_id)
            subject = next(
                i for i in (await c.get("/api/approvals/")).json()["actions"]
                if i["origin"] == "agent_activation"
            )["action_id"]

            stack.as_person("kc-a", "a@example.com", "operator")
            await c.post(f"/api/approvals/{subject}/approve")

            stack.as_person("kc-owner", "owner@example.com", "tenant_owner")
            queue = (await c.get("/api/approvals/")).json()
            assert queue["activation_total"] == 1, "still needs a second approver"
            item = next(
                i for i in queue["actions"] if i["origin"] == "agent_activation"
            )
            # E0.1's progress block, on the third origin too: a second
            # approver has no way to know they are needed without it.
            assert item["approval"]["required"] == 2
            assert item["approval"]["received"] == 1

    @pytest.mark.asyncio
    async def test_an_auditor_reads_the_queue_and_still_decides_nothing(self):
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            agent_id = await _unattended_agent(c, site_id)
            await _preflight(c, agent_id)
            subject = next(
                i for i in (await c.get("/api/approvals/")).json()["actions"]
                if i["origin"] == "agent_activation"
            )["action_id"]

            stack.as_person("kc-au", "au@example.com", "auditor")
            listed = await c.get("/api/approvals/")
            assert listed.status_code == 200
            assert (
                await c.post(f"/api/approvals/{subject}/approve")
            ).status_code == 403


# ---------------------------------------------------------------------------
# What the page renders — served, never derived in the browser
# ---------------------------------------------------------------------------


class TestReadinessReadServesTheWholeAnswer:
    @pytest.mark.asyncio
    async def test_the_readiness_read_reports_its_own_approval_state(self):
        """Otherwise the page must guess whether the gate will let it through."""
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            await c.post("/api/policies/", json={
                "name": "dual", "action_type": "*", "device_type": "*",
                "risk_level": "*", "required_approvers": 2,
            })
            agent_id = await _unattended_agent(c, site_id)
            await _preflight(c, agent_id)

            read = (
                await c.get(f"/api/operational-agents/{agent_id}/preflight")
            ).json()
            block = read["activation_approval"]
            assert block["required"] == 2 and block["received"] == 0
            assert block["state"] == "pending"
            assert block["subject_ref"]

            stack.as_person("kc-a", "a@example.com", "operator")
            await c.post(f"/api/approvals/{block['subject_ref']}/approve")
            stack.as_person("kc-owner", "owner@example.com", "tenant_owner")

            read = (
                await c.get(f"/api/operational-agents/{agent_id}/preflight")
            ).json()
            assert read["activation_approval"]["received"] == 1
            assert read["activation_approval"]["state"] == "pending"

    @pytest.mark.asyncio
    async def test_a_propose_only_agent_reports_no_approval_block(self):
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            res = await c.post("/api/operational-agents/", json=_body(site_id))
            agent_id = res.json()["id"]
            await _preflight(c, agent_id)
            read = (
                await c.get(f"/api/operational-agents/{agent_id}/preflight")
            ).json()
            assert read["activation_approval"] is None

    @pytest.mark.asyncio
    async def test_the_readiness_read_carries_the_acknowledgement(self):
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            res = await c.post("/api/operational-agents/", json=_body(site_id))
            agent_id = res.json()["id"]
            # Raw preflight, so the un-acknowledged state is observable.
            first = await c.post(f"/api/operational-agents/{agent_id}/preflight")
            assert first.json()["requires_acknowledgement"] is True
            read = (
                await c.get(f"/api/operational-agents/{agent_id}/preflight")
            ).json()
            assert read["acknowledged_by"] is None
            assert read["acknowledgement_current"] is False

            await c.post(f"/api/operational-agents/{agent_id}/acknowledge")
            read = (
                await c.get(f"/api/operational-agents/{agent_id}/preflight")
            ).json()
            assert read["acknowledged_by"] == "owner@example.com"
            assert read["acknowledgement_current"] is True

            # A re-run supersedes it: the set of warnings may have changed.
            await c.post(f"/api/operational-agents/{agent_id}/preflight")
            read = (
                await c.get(f"/api/operational-agents/{agent_id}/preflight")
            ).json()
            assert read["acknowledged_by"] is None

    @pytest.mark.asyncio
    async def test_every_dimension_carries_a_sentence_a_person_can_act_on(self):
        """A verdict with no reason is a colour, not an explanation."""
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            agent_id = await _unattended_agent(c, site_id)
            result = await _preflight(c, agent_id)
            assert len(result["dimensions"]) == 12
            for d in result["dimensions"]:
                assert d["detail"], d["dimension"]
                assert d["verdict"] in ("ready", "warn", "blocked", "unknown")


class TestListAndRuntimeAnswerTheOperatorsQuestions:
    @pytest.mark.asyncio
    async def test_the_list_reports_the_running_version_and_drift(self):
        """An operator scanning the list must see drift without opening it."""
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            res = await c.post("/api/operational-agents/", json=_body(site_id))
            agent_id = res.json()["id"]
            await _preflight(c, agent_id)
            await c.post(f"/api/operational-agents/{agent_id}/activate")

            row = next(
                a for a in (await c.get("/api/operational-agents/")).json()["agents"]
                if a["id"] == agent_id
            )
            assert row["activated_version"] == 1
            assert row["configuration_drifted"] is False
            assert row["activation_provenance"] == "recorded"

            await c.patch(f"/api/operational-agents/{agent_id}",
                          json={"description": "now also days"})
            row = next(
                a for a in (await c.get("/api/operational-agents/")).json()["agents"]
                if a["id"] == agent_id
            )
            assert row["version"] == 2 and row["activated_version"] == 1
            assert row["configuration_drifted"] is True

    @pytest.mark.asyncio
    async def test_the_list_reports_budget_and_hold(self):
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            res = await c.post("/api/operational-agents/", json=_body(
                site_id, execution_budget=7, budget_period="weekly",
            ))
            agent_id = res.json()["id"]
            await c.patch(f"/api/operational-agents/{agent_id}",
                          json={"paused_reason": "held by ops"})
            row = next(
                a for a in (await c.get("/api/operational-agents/")).json()["agents"]
                if a["id"] == agent_id
            )
            assert row["execution_budget"] == 7
            assert row["budget_period"] == "weekly"
            assert row["paused_reason"] == "held by ops"

    @pytest.mark.asyncio
    async def test_runtime_shows_skill_delivery_per_device_with_reasons(self):
        """A summary count cannot say WHICH devices missed it, or why."""
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            body = _body(site_id)
            body["capabilities"] = [
                {"kind": "action_class", "capability_ref": "SEL_CLEAR"},
                {"kind": "skill", "capability_ref": "fan-health"},
            ]
            agent_id = (
                await c.post("/api/operational-agents/", json=body)
            ).json()["id"]
            await _preflight(c, agent_id)
            await c.post(f"/api/operational-agents/{agent_id}/activate")

            runtime = (
                await c.get(f"/api/operational-agents/{agent_id}/runtime")
            ).json()
            assert runtime["skills"] == {"queued": 1}
            by_id = runtime["skills_by_id"]
            assert len(by_id) == 1
            assert by_id[0]["skill_id"] == "fan-health"
            devices = by_id[0]["devices"]
            assert [d["device_agent_id"] for d in devices] == ["node-1"]
            assert devices[0]["status"] == "queued"
            assert devices[0]["site_id"] == site_id

    @pytest.mark.asyncio
    async def test_runtime_never_calls_an_unreported_device_healthy(self):
        """Three-valued, always. `active` is not a synonym for `healthy`."""
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            res = await c.post("/api/operational-agents/", json=_body(site_id))
            agent_id = res.json()["id"]
            await _preflight(c, agent_id)
            await c.post(f"/api/operational-agents/{agent_id}/activate")

            runtime = (
                await c.get(f"/api/operational-agents/{agent_id}/runtime")
            ).json()
            d = runtime["devices"]
            assert d["in_scope"] == 1
            # The seeded device has never reported a reading.
            assert d["never_reported"] == 1
            assert d["seen_recently"] == 0 and d["stale"] == 0
            assert d["seen_recently"] + d["stale"] != d["in_scope"], (
                "an unreported device must not be folded into fresh or stale"
            )
            # Active, and the runtime still says nothing about health.
            assert runtime["activation_state"] == "active"
            assert runtime["evaluation"] == "unknown"

    @pytest.mark.asyncio
    async def test_runtime_is_a_read_and_changes_nothing(self):
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            res = await c.post("/api/operational-agents/", json=_body(site_id))
            agent_id = res.json()["id"]
            before = (await c.get(f"/api/operational-agents/{agent_id}")).json()
            for _ in range(3):
                assert (
                    await c.get(f"/api/operational-agents/{agent_id}/runtime")
                ).status_code == 200
            after = (await c.get(f"/api/operational-agents/{agent_id}")).json()
            assert after["agent"]["version"] == before["agent"]["version"]
            assert after["agent"]["status"] == before["agent"]["status"]
