"""A6-1: governed external submission by reference (A24).

The tests that matter most here are not "submission works". They are the
three that pin what submission must never become:

* `TestTheCeilingDoesNotGrant` — `proposal.submit` is IN the ceiling, and
  an agent without an explicit ingress binding still cannot submit. A
  ceiling-driven test would pass while the binding requirement was
  missing, which is exactly the shape A24.4 exists to prevent.
* `TestTheBodyCannotAuthorize` — every governance field is REJECTED, not
  ignored. A22.15 recorded, before this route existed, that a body able
  to carry `authorization_basis` would be a self-signed execution order.
  A schema that silently dropped it would still accept the attempt.
* `TestTheReferenceIsNotALicence` — a reference for work that no longer
  governs matches nothing, however valid it was when issued.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from harkeniq_cc.api.deps import get_current_user
from harkeniq_cc.app import create_app
from harkeniq_cc.auth import ROLE_PERMISSIONS, UserContext, configure_auth
from harkeniq_cc.config import CCConfig
from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_cc.db.models import (
    CCAgentProposal,
    CCAgentSubmission,
    CCAutonomyBudget,
    CCFleetCache,
    CCIncident,
    CCOperationalAgent,
    CCSite,
)
from harkeniq_cc.machine_identity import (
    INGRESS_BINDING_PERMISSIONS,
    MACHINE_PRINCIPAL_CEILING,
    machine_permissions,
)
from harkeniq_cc.runtime import AppState

from tests.unit.cc.conftest import seed_tenant_admin

TENANT = "t1"
SUBMIT = "proposal.submit"


class Stack:
    def __init__(self, app, state):
        self.app, self.state = app, state
        self.sessionmaker = state.sessionmaker
        self.persona = ("kc-owner", "owner@example.com", "tenant_owner")
        self.machine = None

    def as_machine(self, agent_id, permissions=(SUBMIT, "fleet.view")):
        self.machine = (agent_id, list(permissions))
        return self

    def as_person(self, sub="kc-owner", email="owner@example.com",
                  role="tenant_owner"):
        self.persona, self.machine = (sub, email, role), None
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
    state = AppState(
        config=config, engine=engine, sessionmaker=make_sessionmaker(engine),
    )
    app = create_app(state)
    stack = Stack(app, state)
    await seed_tenant_admin(state.sessionmaker, TENANT, "kc-owner")

    async def _fake():
        if stack.machine is not None:
            agent_id, perms = stack.machine
            return UserContext(
                user_id=agent_id, email=f"op-agent:{agent_id}@v1",
                tenant_id=TENANT, role="", permissions=perms,
                species="agent", identity_id="id-1",
            )
        sub, email, role = stack.persona
        return UserContext(
            user_id=sub, email=email, tenant_id=TENANT, role=role,
            permissions=list(ROLE_PERMISSIONS[role]),
        )

    app.dependency_overrides[get_current_user] = _fake
    return stack


async def _seed(stack, *, level=2) -> str:
    async with stack.sessionmaker() as session:
        session.add(CCAutonomyBudget(
            tenant_id=TENANT, device_type="*", level=level,
            budget_limit=100, budget_period="daily",
        ))
        site = CCSite(tenant_id=TENANT, site_name="DC-1",
                      sm_endpoint="sm:50051", sm_token="tok")
        session.add(site)
        await session.flush()
        session.add(CCFleetCache(
            site_id=site.id, agent_id="node-1", agent_name="n1",
            vendor="Dell", model="R750", device_class="server",
            observation="observed", health="Critical",
            capabilities={
                "reach_known": True,
                "implemented": ["SEL_CLEAR", "COLLECT_DIAGNOSTICS",
                                "IDENTIFY_LED"],
                "allowed": ["SEL_CLEAR", "COLLECT_DIAGNOSTICS", "IDENTIFY_LED"],
                "effective": ["SEL_CLEAR", "COLLECT_DIAGNOSTICS",
                              "IDENTIFY_LED"],
            },
        ))
        session.add(CCIncident(
            incident_id="inc-1", tenant_id=TENANT, site_id=site.id,
            kind="device", status="open", title="disk failing on n1",
            device_agent_id="node-1", subsystem="disk", confidence=0.9,
            components=[{"component": "Disk.Bay.1", "severity": "CRITICAL"}],
        ))
        await session.commit()
        return site.id


async def _agent(client, site_id, *, ingress=True, name="Night Shift") -> str:
    caps = [{"kind": "action_class", "capability_ref": "IDENTIFY_LED"}]
    if ingress:
        caps.append({"kind": "ingress", "capability_ref": "proposals"})
    res = await client.post("/api/operational-agents/", json={
        "name": name,
        "scopes": [{"scope_type": "site", "scope_ref": site_id}],
        "capabilities": caps,
    })
    assert res.status_code == 201, res.text
    return res.json()["id"]


async def _activate(stack, agent_id):
    async with stack.sessionmaker() as session:
        agent = await session.get(CCOperationalAgent, agent_id)
        agent.status = "active"
        agent.activated_version = agent.version
        await session.commit()


async def _ready(stack) -> tuple[str, str]:
    """A live agent with an ingress binding, and one governed candidate."""
    site_id = await _seed(stack)
    async with stack.client() as c:
        agent_id = await _agent(c, site_id)
    await _activate(stack, agent_id)
    return agent_id, site_id


async def _first_ref(stack, agent_id) -> str:
    async with stack.as_machine(agent_id).client() as c:
        res = await c.get(f"/api/operational-agents/{agent_id}/dry-run")
        assert res.status_code == 200, res.text
        would = res.json()["would_propose"]
        assert would, "fixture produced no governed candidate"
        return would[0]["candidate_ref"]


def _body(ref, key="idem-0001-aaaa", **kw):
    out = {"candidate_ref": ref, "idempotency_key": key}
    out.update(kw)
    return out


# ---------------------------------------------------------------------------
# 1. The ceiling admits; only a binding grants (A24.4)
# ---------------------------------------------------------------------------


class TestTheCeilingDoesNotGrant:
    def test_the_ceiling_admits_the_permission(self):
        assert SUBMIT in MACHINE_PRINCIPAL_CEILING

    def test_no_read_binding_can_ever_imply_it(self):
        """The one write must be unreachable from every read binding."""
        from harkeniq_cc.machine_identity import READ_BINDING_PERMISSIONS

        assert SUBMIT not in machine_permissions(READ_BINDING_PERMISSIONS)

    def test_every_read_binding_together_still_cannot_submit(self):
        from harkeniq_cc.machine_identity import READ_BINDING_PERMISSIONS

        assert SUBMIT not in machine_permissions(
            list(READ_BINDING_PERMISSIONS), ingress_bindings=()
        )

    def test_an_ingress_binding_grants_it(self):
        assert SUBMIT in machine_permissions((), list(INGRESS_BINDING_PERMISSIONS))

    def test_the_result_never_escapes_the_ceiling(self):
        """Whatever the binding tables grow to hold."""
        granted = set(machine_permissions(
            ["attention", "fleet", "incidents", "autonomy", "learning",
             "nonsense"],
            ["proposals", "nonsense"],
        ))
        assert granted <= set(MACHINE_PRINCIPAL_CEILING)
        assert "*" not in granted

    def test_no_human_role_holds_it(self):
        """A24.4: a machine permission, and only a machine permission."""
        for role, perms in ROLE_PERMISSIONS.items():
            if "*" in perms:
                continue
            assert SUBMIT not in perms, f"{role} holds a machine permission"

    async def test_an_agent_without_the_binding_is_refused(self):
        """THE test. The ceiling permits the class; this agent still cannot."""
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            agent_id = await _agent(c, site_id, ingress=False)
        await _activate(stack, agent_id)

        # Resolved the way `auth.py` resolves it, from the agent's own rows.
        from harkeniq_cc.db.repos import OperationalAgentRepo
        from harkeniq_cc.operational_agent import bound_ingress, bound_reads

        async with stack.sessionmaker() as session:
            caps = await OperationalAgentRepo(session).list_capabilities(agent_id)
        assert SUBMIT not in machine_permissions(
            bound_reads(caps), bound_ingress(caps)
        )

    async def test_an_agent_with_the_binding_holds_it(self):
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        from harkeniq_cc.db.repos import OperationalAgentRepo
        from harkeniq_cc.operational_agent import bound_ingress, bound_reads

        async with stack.sessionmaker() as session:
            caps = await OperationalAgentRepo(session).list_capabilities(agent_id)
        assert SUBMIT in machine_permissions(bound_reads(caps), bound_ingress(caps))


# ---------------------------------------------------------------------------
# 2. The body cannot authorize anything (A24.2 / A22.15)
# ---------------------------------------------------------------------------


class TestTheBodyCannotAuthorize:
    @pytest.mark.parametrize("field,value", [
        ("agent_id", "other-agent"),
        ("action_type", "POWER_CYCLE"),
        ("device_agent_id", "node-9"),
        ("params", {"target": "anything"}),
        ("disposition", "autonomous"),
        ("authorization_basis", "autonomous_grant"),
        ("status", "approved"),
        ("decided_by", "autonomy:level-9"),
        ("site_id", "elsewhere"),
        ("autonomy_level", 3),
    ])
    async def test_a_governance_field_is_rejected_not_ignored(self, field, value):
        """Rejected. Silently dropping it would still accept the attempt."""
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        ref = await _first_ref(stack, agent_id)
        async with stack.as_machine(agent_id).client() as c:
            res = await c.post(
                f"/api/operational-agents/{agent_id}/proposals",
                json=_body(ref, **{field: value}),
            )
        assert res.status_code == 422, (
            f"{field!r} was accepted by the transport schema"
        )

    async def test_the_four_declared_fields_are_accepted(self):
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        ref = await _first_ref(stack, agent_id)
        async with stack.as_machine(agent_id).client() as c:
            res = await c.post(
                f"/api/operational-agents/{agent_id}/proposals",
                json=_body(
                    ref,
                    observed_at=datetime.now(timezone.utc).isoformat(),
                    note="seen twice in ten minutes",
                ),
            )
        assert res.status_code == 201, res.text

    async def test_the_note_is_bounded(self):
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        ref = await _first_ref(stack, agent_id)
        async with stack.as_machine(agent_id).client() as c:
            res = await c.post(
                f"/api/operational-agents/{agent_id}/proposals",
                json=_body(ref, note="x" * 4000),
            )
        assert res.status_code == 422


# ---------------------------------------------------------------------------
# 3. A machine agent acts as itself (A24.5)
# ---------------------------------------------------------------------------


class TestAMachineActsAsItself:
    async def test_an_agent_cannot_submit_for_another_agent(self):
        stack = await _stack()
        agent_id, site_id = await _ready(stack)
        async with stack.client() as c:
            other = await _agent(c, site_id, name="Day Shift")
        ref = await _first_ref(stack, agent_id)
        async with stack.as_machine(agent_id).client() as c:
            res = await c.post(
                f"/api/operational-agents/{other}/proposals", json=_body(ref)
            )
        assert res.status_code == 403
        assert "its own agent" in res.json()["detail"]

    async def test_a_person_holding_the_wildcard_is_still_refused(self):
        """A human has no agent to act as. Refused in its own words."""
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        ref = await _first_ref(stack, agent_id)
        stack.as_person(role="platform_super_admin")
        async with stack.client() as c:
            res = await c.post(
                f"/api/operational-agents/{agent_id}/proposals", json=_body(ref)
            )
        assert res.status_code == 403
        assert "machine-principal surface" in res.json()["detail"]

    async def test_no_proposal_and_no_submission_survives_a_refusal(self):
        stack = await _stack()
        agent_id, site_id = await _ready(stack)
        async with stack.client() as c:
            other = await _agent(c, site_id, name="Day Shift")
        ref = await _first_ref(stack, agent_id)
        async with stack.as_machine(agent_id).client() as c:
            await c.post(
                f"/api/operational-agents/{other}/proposals", json=_body(ref)
            )
        async with stack.sessionmaker() as session:
            assert (await session.execute(
                __import__("sqlalchemy").select(CCAgentSubmission)
            )).scalars().all() == []


# ---------------------------------------------------------------------------
# 4. The reference is a lookup, never a licence (A24.3)
# ---------------------------------------------------------------------------


class TestTheReferenceIsNotALicence:
    async def test_an_unknown_reference_creates_nothing(self):
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        async with stack.as_machine(agent_id).client() as c:
            res = await c.post(
                f"/api/operational-agents/{agent_id}/proposals",
                json=_body("cand_" + "0" * 32),
            )
        assert res.status_code == 409
        assert res.json()["code"] == "candidate_not_current"
        assert res.json()["accepted"] is False
        async with stack.sessionmaker() as session:
            import sqlalchemy as sa
            assert (await session.execute(
                sa.select(CCAgentProposal)
            )).scalars().all() == []

    async def test_a_reference_stops_working_once_the_condition_clears(self):
        """Valid when issued is not valid now."""
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        ref = await _first_ref(stack, agent_id)

        async with stack.sessionmaker() as session:
            inc = await session.get(CCIncident, "inc-1")
            inc.status = "resolved"
            await session.commit()

        async with stack.as_machine(agent_id).client() as c:
            res = await c.post(
                f"/api/operational-agents/{agent_id}/proposals", json=_body(ref)
            )
        assert res.status_code == 409
        assert res.json()["code"] == "candidate_not_current"

    async def test_a_refused_reference_is_still_recorded(self):
        """A refusal that left no row would be free to repeat."""
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        async with stack.as_machine(agent_id).client() as c:
            await c.post(
                f"/api/operational-agents/{agent_id}/proposals",
                json=_body("cand_" + "1" * 32),
            )
        async with stack.sessionmaker() as session:
            import sqlalchemy as sa
            rows = (await session.execute(
                sa.select(CCAgentSubmission)
            )).scalars().all()
        assert len(rows) == 1 and rows[0].proposal_id is None


# ---------------------------------------------------------------------------
# 5. Replay (A24.2) and logical duplication (A24.6) are two guarantees
# ---------------------------------------------------------------------------


class TestReplayAndDuplication:
    async def test_the_same_key_and_payload_returns_the_same_proposal(self):
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        ref = await _first_ref(stack, agent_id)
        async with stack.as_machine(agent_id).client() as c:
            first = await c.post(
                f"/api/operational-agents/{agent_id}/proposals", json=_body(ref)
            )
            second = await c.post(
                f"/api/operational-agents/{agent_id}/proposals", json=_body(ref)
            )
        assert first.status_code == 201
        assert second.status_code == 200, "a replay must not be a creation"
        assert second.json()["replayed"] is True
        assert first.json()["proposal_id"] == second.json()["proposal_id"]

        async with stack.sessionmaker() as session:
            import sqlalchemy as sa
            assert len((await session.execute(
                sa.select(CCAgentProposal)
            )).scalars().all()) == 1

    async def test_the_same_key_with_different_work_is_a_conflict(self):
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        ref = await _first_ref(stack, agent_id)
        async with stack.as_machine(agent_id).client() as c:
            await c.post(
                f"/api/operational-agents/{agent_id}/proposals", json=_body(ref)
            )
            res = await c.post(
                f"/api/operational-agents/{agent_id}/proposals",
                json=_body("cand_" + "2" * 32, key="idem-0001-aaaa"),
            )
        assert res.status_code == 409
        assert "already used for a different" in res.json()["detail"]

    async def test_a_different_key_for_the_same_candidate_does_not_duplicate(self):
        """A24.6. The replay key cannot see this; the admission path can."""
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        ref = await _first_ref(stack, agent_id)
        async with stack.as_machine(agent_id).client() as c:
            first = await c.post(
                f"/api/operational-agents/{agent_id}/proposals",
                json=_body(ref, key="idem-key-one"),
            )
            second = await c.post(
                f"/api/operational-agents/{agent_id}/proposals",
                json=_body(ref, key="idem-key-two"),
            )
        assert first.status_code == 201
        assert second.status_code == 409, "a second key created a second proposal"
        async with stack.sessionmaker() as session:
            import sqlalchemy as sa
            assert len((await session.execute(
                sa.select(CCAgentProposal)
            )).scalars().all()) == 1

    async def test_the_unique_constraint_is_the_guarantee(self):
        """Idempotency is a DB fact, not a convention the route maintains."""
        cols = {
            tuple(sorted(c.name for c in con.columns))
            for con in CCAgentSubmission.__table__.constraints
            if con.__class__.__name__ == "UniqueConstraint"
        }
        assert ("agent_id", "idempotency_key", "tenant_id") in cols


# ---------------------------------------------------------------------------
# 6. One admission path (A24.6)
# ---------------------------------------------------------------------------


class TestOneAdmissionPath:
    def test_the_evaluator_admits_through_the_shared_function(self):
        """Asserted over the source, not by behaviour that could agree today."""
        import inspect

        from harkeniq_cc import agent_runtime

        src = inspect.getsource(agent_runtime.evaluate_agents)
        assert "admit_proposal(" in src
        assert "prop_repo.create(" not in src, (
            "the evaluator creates proposals outside the shared admission path"
        )

    def test_the_ingress_admits_through_the_shared_function(self):
        import inspect

        from harkeniq_cc.api import operational_agents

        src = inspect.getsource(operational_agents.submit_proposal)
        assert "admit_proposal(" in src
        assert "AgentProposalRepo(session).create" not in src

    def test_the_ingress_reuses_the_one_verdict_function(self):
        """No second reasoning path: it re-runs `evaluate`, like dry-run."""
        import inspect

        from harkeniq_cc.api import operational_agents

        src = inspect.getsource(operational_agents.submit_proposal)
        assert "evaluate(" in src
        assert "govern_proposal" not in src, (
            "ingress should reach the verdict through evaluate(), not "
            "re-implement candidate selection"
        )

    async def test_admission_refuses_a_key_already_admitted(self):
        from harkeniq_cc.proposal_admission import CODE_DUPLICATE, admit_proposal

        stack = await _stack()
        agent_id, site_id = await _ready(stack)
        payload = {
            "tenant_id": TENANT, "agent_id": agent_id,
            "actor": f"op-agent:{agent_id}@v1", "agent_version": 1,
            "site_id": site_id, "device_agent_id": "node-1",
            "action_type": "IDENTIFY_LED", "params": {"target": "x"},
            "rationale": "r", "evidence": {}, "disposition": "requires_approval",
            "disposition_reason": "", "blocking_conditions": [],
            "authorization_basis": "human_approval", "status": "awaiting_approval",
            "decided_by": "", "decided_at": None, "dedupe_key": "dk-1",
        }
        async with stack.sessionmaker() as session:
            row, code, _ = await admit_proposal(
                session, tenant_id=TENANT, payload=payload, origin="ingress",
            )
            assert row is not None and code == ""
            await session.commit()
        async with stack.sessionmaker() as session:
            row2, code2, _ = await admit_proposal(
                session, tenant_id=TENANT, payload=payload, origin="ingress",
            )
        assert row2 is None and code2 == CODE_DUPLICATE


# ---------------------------------------------------------------------------
# 7. Budget, rate and lifecycle (A24.7 / A24.8)
# ---------------------------------------------------------------------------


class TestBudgetAndRate:
    async def test_submission_spends_no_execution_budget(self):
        """A24.7: intent is not consumption."""
        from harkeniq_cc.agent_lifecycle import executions_used

        stack = await _stack()
        agent_id, _ = await _ready(stack)
        async with stack.sessionmaker() as session:
            agent = await session.get(CCOperationalAgent, agent_id)
            agent.execution_budget = 5
            await session.commit()
        ref = await _first_ref(stack, agent_id)
        async with stack.as_machine(agent_id).client() as c:
            assert (await c.post(
                f"/api/operational-agents/{agent_id}/proposals", json=_body(ref)
            )).status_code == 201
        async with stack.sessionmaker() as session:
            agent = await session.get(CCOperationalAgent, agent_id)
            assert await executions_used(session, TENANT, agent) == 0

    async def test_an_exhausted_execution_budget_still_permits_submission(self):
        from harkeniq_cc.agent_activation import unattended_permitted

        stack = await _stack()
        agent_id, _ = await _ready(stack)
        async with stack.sessionmaker() as session:
            agent = await session.get(CCOperationalAgent, agent_id)
            agent.execution_budget = 1
            await session.commit()
            # Spent, by the authoritative check's own reading.
            assert unattended_permitted(agent, 1)[0] is False
        ref = await _first_ref(stack, agent_id)
        async with stack.as_machine(agent_id).client() as c:
            res = await c.post(
                f"/api/operational-agents/{agent_id}/proposals", json=_body(ref)
            )
        assert res.status_code == 201

    async def test_the_rate_window_counts_committed_rows(self):
        """A24.8: durable, so it is true on a multi-replica CC."""
        from harkeniq_cc.db.repos import AgentSubmissionRepo

        stack = await _stack()
        agent_id, _ = await _ready(stack)
        async with stack.sessionmaker() as session:
            repo = AgentSubmissionRepo(session)
            for n in range(3):
                await repo.record(
                    tenant_id=TENANT, agent_id=agent_id, agent_version=1,
                    idempotency_key=f"k{n}", request_digest="d",
                    candidate_ref="c", proposal_id=None, code="x",
                )
            await session.commit()
        async with stack.sessionmaker() as session:
            since = datetime.now(timezone.utc) - timedelta(hours=1)
            assert await AgentSubmissionRepo(session).count_since(
                TENANT, agent_id, since
            ) == 3

    async def test_the_limiter_is_not_an_in_process_counter(self):
        """A per-process counter would be decorative on a multi-replica CC."""
        import inspect

        from harkeniq_cc.api import operational_agents

        src = inspect.getsource(operational_agents.submit_proposal)
        assert "count_since(" in src

    @pytest.mark.parametrize("status", ["draft", "paused", "retired"])
    async def test_an_agent_that_is_not_active_may_not_submit(self, status):
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        ref = await _first_ref(stack, agent_id)
        async with stack.sessionmaker() as session:
            agent = await session.get(CCOperationalAgent, agent_id)
            agent.status = status
            await session.commit()
        async with stack.as_machine(agent_id).client() as c:
            res = await c.post(
                f"/api/operational-agents/{agent_id}/proposals", json=_body(ref)
            )
        assert res.status_code == 409

    async def test_a_paused_agent_may_not_submit(self):
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        ref = await _first_ref(stack, agent_id)
        async with stack.sessionmaker() as session:
            agent = await session.get(CCOperationalAgent, agent_id)
            agent.paused_reason = "operator paused"
            await session.commit()
        async with stack.as_machine(agent_id).client() as c:
            res = await c.post(
                f"/api/operational-agents/{agent_id}/proposals", json=_body(ref)
            )
        assert res.status_code == 409
        assert "paused" in res.json()["detail"]


# ---------------------------------------------------------------------------
# 8. What an accepted submission actually is
# ---------------------------------------------------------------------------


class TestWhatAcceptanceMeans:
    async def test_an_accepted_submission_still_requires_a_human(self):
        """201 means a proposal exists. It never means anything will run."""
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        ref = await _first_ref(stack, agent_id)
        async with stack.as_machine(agent_id).client() as c:
            res = await c.post(
                f"/api/operational-agents/{agent_id}/proposals", json=_body(ref)
            )
        body = res.json()
        assert body["accepted"] is True
        assert body["proposal"]["status"] == "awaiting_approval"
        assert body["proposal"]["requires_approval"] is True
        assert "confers nothing" in body["governs"]

    async def test_the_proposal_carries_derived_parameters(self):
        """A22.3 holds: params come from the platform, never the caller."""
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        ref = await _first_ref(stack, agent_id)
        async with stack.as_machine(agent_id).client() as c:
            res = await c.post(
                f"/api/operational-agents/{agent_id}/proposals", json=_body(ref)
            )
        params = res.json()["proposal"]["params"]
        assert params.get("target") == "Disk.Bay.1", (
            "the component came from the reported condition, not the body"
        )

    async def test_the_origin_is_recorded_as_ingress(self):
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        ref = await _first_ref(stack, agent_id)
        async with stack.as_machine(agent_id).client() as c:
            await c.post(
                f"/api/operational-agents/{agent_id}/proposals", json=_body(ref)
            )
        async with stack.sessionmaker() as session:
            import sqlalchemy as sa
            from harkeniq_cc.db.models import CCAuditLog
            rows = (await session.execute(
                sa.select(CCAuditLog).where(
                    CCAuditLog.action == "agent_proposal.created"
                )
            )).scalars().all()
        assert rows and rows[0].detail.get("origin") == "ingress"

    async def test_every_submission_is_audited_with_a_stable_actor_ref(self):
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        ref = await _first_ref(stack, agent_id)
        async with stack.as_machine(agent_id).client() as c:
            await c.post(
                f"/api/operational-agents/{agent_id}/proposals", json=_body(ref)
            )
        async with stack.sessionmaker() as session:
            import sqlalchemy as sa
            from harkeniq_cc.db.models import CCAuditLog
            rows = (await session.execute(
                sa.select(CCAuditLog).where(
                    CCAuditLog.action == "agent_submission.accepted"
                )
            )).scalars().all()
        assert len(rows) == 1
        assert rows[0].actor_ref == agent_id

    async def test_the_audit_chain_still_verifies(self):
        from harkeniq_cc.db.repos import AuditRepo

        stack = await _stack()
        agent_id, _ = await _ready(stack)
        ref = await _first_ref(stack, agent_id)
        async with stack.as_machine(agent_id).client() as c:
            await c.post(
                f"/api/operational-agents/{agent_id}/proposals", json=_body(ref)
            )
        async with stack.sessionmaker() as session:
            assert (await AuditRepo(session).verify_chain()).valid

    def test_every_agent_necessarily_holds_the_permission_the_gate_needs(self):
        """The object gate asks `fleet.view`; nothing may starve it.

        `_require_visible_agent` resolves with `fleet.view`, so an agent
        holding only `proposal.submit` would 404 on its own agent -- able
        to submit in principle and unable to be seen. It cannot happen:
        `REQUIRED_READS` is forced onto every agent at creation and every
        one of those reads implies `fleet.view`.

        That is an implicit coupling between two modules, so it is pinned
        here rather than left to be rediscovered by whoever edits
        REQUIRED_READS next.
        """
        from harkeniq_cc.machine_identity import READ_BINDING_PERMISSIONS
        from harkeniq_cc.operational_agent import REQUIRED_READS

        implied = set()
        for read in REQUIRED_READS:
            implied |= READ_BINDING_PERMISSIONS.get(read, frozenset())
        assert "fleet.view" in implied, (
            "no required read implies fleet.view, so an agent bound only to "
            "ingress would be refused by the object gate on its own agent"
        )

    async def test_an_unknown_ingress_ref_is_refused_not_left_inert(self):
        """An unrecognised ref maps to no permission: accepted, it would
        be an agent configured to submit that silently cannot."""
        stack = await _stack()
        site_id = await _seed(stack)
        async with stack.client() as c:
            res = await c.post("/api/operational-agents/", json={
                "name": "Typo Shift",
                "scopes": [{"scope_type": "site", "scope_ref": site_id}],
                "capabilities": [
                    {"kind": "action_class", "capability_ref": "IDENTIFY_LED"},
                    {"kind": "ingress", "capability_ref": "propossals"},
                ],
            })
        assert res.status_code == 400
        assert "governed ingress capability" in res.json()["detail"]

    async def test_the_catalogue_offers_the_ingress_binding(self):
        """A binding nobody can discover is a binding nobody can create."""
        stack = await _stack()
        await _seed(stack)
        async with stack.client() as c:
            res = await c.get("/api/operational-agents/catalogue")
        refs = [r["ref"] for r in res.json()["ingress_capabilities"]]
        assert "proposals" in refs
