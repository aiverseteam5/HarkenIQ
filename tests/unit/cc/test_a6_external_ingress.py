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


async def _revoke_agent_scope(stack, agent_id: str) -> None:
    """Withdraw an agent's reach the way an operator does.

    E1.2 migrated `cc_agent_scopes` into `cc_scope_grants` as
    `principal_type="agent"`, and A23-3 made withdrawal a revocation
    rather than a delete, so a grant that once existed stays visible as
    evidence.
    """
    import sqlalchemy as sa

    from harkeniq_cc.db.models import CCScopeGrant

    async with stack.sessionmaker() as session:
        await session.execute(
            sa.update(CCScopeGrant)
            .where(
                CCScopeGrant.principal_type == "agent",
                CCScopeGrant.principal_ref == agent_id,
            )
            .values(revoked_at=datetime.now(timezone.utc))
        )
        await session.commit()


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


# ---------------------------------------------------------------------------
# 9. Pre-merge red-team fixes (A24.11-A24.16)
# ---------------------------------------------------------------------------


class TestOperationalCollisionIdentity:
    """A24.12: attribution is not operational identity."""

    def test_the_identity_comes_from_the_contract_not_a_constant(self):
        """Component-addressing params identify; annotations do not."""
        from harkeniq.autonomy.preconditions import (
            ACTION_PARAMETERS, SRC_COMPONENT,
        )
        from harkeniq.capabilities import operation_identity
        from harkeniq.models import ActionType

        for action, specs in ACTION_PARAMETERS.items():
            addressing = [s.name for s in specs if s.source == SRC_COMPONENT]
            identity = operation_identity(action.value, {n: "X" for n in addressing})
            for name in addressing:
                assert f"{name}=X" in identity, (action, name)

    def test_the_reason_never_changes_the_operation(self):
        from harkeniq.capabilities import operation_key

        a = operation_key("t", "node-1", "SEL_CLEAR", {"reason": "one"})
        b = operation_key("t", "node-1", "SEL_CLEAR", {"reason": "two"})
        assert a == b

    def test_two_agents_naming_one_operation_agree(self):
        from harkeniq.capabilities import operation_key

        assert operation_key(
            "t", "node-1", "IDENTIFY_LED", {"target": "Disk.Bay.1"}
        ) == operation_key(
            "t", "node-1", "IDENTIFY_LED",
            {"target": "Disk.Bay.1", "reason": "a different agent"},
        )

    def test_different_operations_stay_distinct(self):
        from harkeniq.capabilities import operation_key

        base = operation_key("t", "node-1", "IDENTIFY_LED", {"target": "Bay.1"})
        assert base != operation_key(
            "t", "node-1", "IDENTIFY_LED", {"target": "Bay.2"})
        assert base != operation_key(
            "t", "node-2", "IDENTIFY_LED", {"target": "Bay.1"})
        assert base != operation_key("t", "node-1", "SEL_CLEAR", {})

    async def test_a_second_agent_cannot_open_the_same_operation(self):
        """THE invariant: one physical operation, one open proposal."""
        from harkeniq_cc.proposal_admission import (
            CODE_OPERATION_IN_FLIGHT, admit_proposal,
        )

        stack = await _stack()
        agent_a, site_id = await _ready(stack)
        async with stack.client() as c:
            agent_b = await _agent(c, site_id, name="Second Shift")

        def payload(agent_id):
            return {
                "tenant_id": TENANT, "agent_id": agent_id,
                "actor": f"op-agent:{agent_id}@v1", "agent_version": 1,
                "site_id": site_id, "device_agent_id": "node-1",
                "action_type": "IDENTIFY_LED",
                "params": {"target": "Disk.Bay.1"},
                "rationale": "r", "evidence": {},
                "disposition": "requires_approval", "disposition_reason": "",
                "blocking_conditions": [],
                "authorization_basis": "human_approval",
                "status": "awaiting_approval", "decided_by": "",
                "decided_at": None,
                # Different agents => DIFFERENT dedupe keys. Nothing the
                # per-agent rule can see.
                "dedupe_key": f"{agent_id}:node-1:IDENTIFY_LED:inc-1:Disk.Bay.1",
            }

        async with stack.sessionmaker() as session:
            row, code, _ = await admit_proposal(
                session, tenant_id=TENANT, payload=payload(agent_a),
                origin="ingress",
            )
            assert row is not None and code == ""
            await session.commit()
        async with stack.sessionmaker() as session:
            row2, code2, _ = await admit_proposal(
                session, tenant_id=TENANT, payload=payload(agent_b),
                origin="ingress",
            )
        assert row2 is None
        assert code2 == CODE_OPERATION_IN_FLIGHT

    async def test_a_settled_operation_does_not_fence_the_device_forever(self):
        from harkeniq_cc.db.models import CCAgentProposal
        from harkeniq_cc.proposal_admission import admit_proposal

        stack = await _stack()
        agent_a, site_id = await _ready(stack)

        def payload(key):
            return {
                "tenant_id": TENANT, "agent_id": agent_a,
                "actor": f"op-agent:{agent_a}@v1", "agent_version": 1,
                "site_id": site_id, "device_agent_id": "node-1",
                "action_type": "IDENTIFY_LED",
                "params": {"target": "Disk.Bay.1"}, "rationale": "r",
                "evidence": {}, "disposition": "requires_approval",
                "disposition_reason": "", "blocking_conditions": [],
                "authorization_basis": "human_approval",
                "status": "awaiting_approval", "decided_by": "",
                "decided_at": None, "dedupe_key": key,
            }

        async with stack.sessionmaker() as session:
            row, _, _ = await admit_proposal(
                session, tenant_id=TENANT, payload=payload("k-1"),
                origin="ingress")
            await session.commit()
            first_id = row.id
        async with stack.sessionmaker() as session:
            settled = await session.get(CCAgentProposal, first_id)
            settled.status = "completed"
            await session.commit()
        async with stack.sessionmaker() as session:
            row2, code2, _ = await admit_proposal(
                session, tenant_id=TENANT, payload=payload("k-2"),
                origin="ingress")
        assert row2 is not None, f"a completed operation still fenced: {code2}"

    async def test_a_historical_proposal_without_a_key_fences_nothing(self):
        """NULL means 'not comparable', never 'equal'."""
        from harkeniq_cc.db.models import CCAgentProposal
        from harkeniq_cc.proposal_admission import admit_proposal

        stack = await _stack()
        agent_a, site_id = await _ready(stack)
        async with stack.sessionmaker() as session:
            session.add(CCAgentProposal(
                tenant_id=TENANT, agent_id=agent_a, actor="legacy",
                site_id=site_id, device_agent_id="node-1",
                action_type="IDENTIFY_LED", params={"target": "Disk.Bay.1"},
                status="awaiting_approval", dedupe_key="legacy-1",
                operation_key=None,
            ))
            await session.commit()
        async with stack.sessionmaker() as session:
            row, code, _ = await admit_proposal(
                session, tenant_id=TENANT,
                payload={
                    "tenant_id": TENANT, "agent_id": agent_a,
                    "actor": f"op-agent:{agent_a}@v1", "agent_version": 1,
                    "site_id": site_id, "device_agent_id": "node-1",
                    "action_type": "IDENTIFY_LED",
                    "params": {"target": "Disk.Bay.1"}, "rationale": "r",
                    "evidence": {}, "disposition": "requires_approval",
                    "disposition_reason": "", "blocking_conditions": [],
                    "authorization_basis": "human_approval",
                    "status": "awaiting_approval", "decided_by": "",
                    "decided_at": None, "dedupe_key": "new-1",
                },
                origin="ingress")
        assert row is not None, f"a NULL operation_key fenced a new proposal: {code}"


class TestAttemptRate:
    """A24.13: every attempt counts, and the count is atomic."""

    async def test_a_replay_is_metered(self):
        """The first implementation returned replays before the counter."""
        from harkeniq_cc.db.repos import AgentIngressAttemptRepo

        stack = await _stack()
        agent_id, _ = await _ready(stack)
        ref = await _first_ref(stack, agent_id)
        async with stack.as_machine(agent_id).client() as c:
            await c.post(f"/api/operational-agents/{agent_id}/proposals",
                         json=_body(ref))
            await c.post(f"/api/operational-agents/{agent_id}/proposals",
                         json=_body(ref))
        async with stack.sessionmaker() as session:
            since = datetime.now(timezone.utc) - timedelta(hours=1)
            assert await AgentIngressAttemptRepo(session).count_since(
                TENANT, agent_id, since
            ) == 2, "a replay did not count toward the attempt rate"

    async def test_a_rejected_candidate_is_metered(self):
        from harkeniq_cc.db.repos import AgentIngressAttemptRepo

        stack = await _stack()
        agent_id, _ = await _ready(stack)
        async with stack.as_machine(agent_id).client() as c:
            await c.post(f"/api/operational-agents/{agent_id}/proposals",
                         json=_body("cand_" + "9" * 32))
        async with stack.sessionmaker() as session:
            since = datetime.now(timezone.utc) - timedelta(hours=1)
            assert await AgentIngressAttemptRepo(session).count_since(
                TENANT, agent_id, since
            ) == 1

    async def test_over_the_limit_is_refused_without_growing_the_record(self):
        """The record must not be grown by the traffic it bounds."""
        from harkeniq_cc.db.repos import AgentIngressAttemptRepo
        from harkeniq_cc.ingress_limits import ATTEMPT_MAX

        stack = await _stack()
        agent_id, _ = await _ready(stack)
        ref = await _first_ref(stack, agent_id)
        async with stack.sessionmaker() as session:
            repo = AgentIngressAttemptRepo(session)
            for _ in range(ATTEMPT_MAX):
                await repo.record(
                    tenant_id=TENANT, agent_id=agent_id, outcome="accepted")
            await session.commit()
        async with stack.as_machine(agent_id).client() as c:
            res = await c.post(f"/api/operational-agents/{agent_id}/proposals",
                               json=_body(ref))
        assert res.status_code == 429
        async with stack.sessionmaker() as session:
            since = datetime.now(timezone.utc) - timedelta(hours=1)
            assert await AgentIngressAttemptRepo(session).count_since(
                TENANT, agent_id, since
            ) == ATTEMPT_MAX, "a refused request added to the record"

    async def test_another_agent_is_not_metered_by_this_one(self):
        from harkeniq_cc.db.repos import AgentIngressAttemptRepo
        from harkeniq_cc.ingress_limits import ATTEMPT_MAX

        stack = await _stack()
        agent_a, site_id = await _ready(stack)
        async with stack.client() as c:
            agent_b = await _agent(c, site_id, name="Other Shift")
        async with stack.sessionmaker() as session:
            repo = AgentIngressAttemptRepo(session)
            for _ in range(ATTEMPT_MAX):
                await repo.record(
                    tenant_id=TENANT, agent_id=agent_a, outcome="accepted")
            await session.commit()
            since = datetime.now(timezone.utc) - timedelta(hours=1)
            assert await repo.count_since(TENANT, agent_b, since) == 0


class TestBodyCeiling:
    """A24.14: bounded before parsing, not after."""

    async def test_an_oversized_body_is_refused_before_it_is_parsed(self):
        from harkeniq_cc.ingress_body import MAX_INGRESS_BODY_BYTES

        stack = await _stack()
        agent_id, _ = await _ready(stack)
        async with stack.as_machine(agent_id).client() as c:
            res = await c.post(
                f"/api/operational-agents/{agent_id}/proposals",
                content=b'{"candidate_ref":"cand_x","idempotency_key":"k",'
                        b'"note":"' + b"x" * (MAX_INGRESS_BODY_BYTES * 2) + b'"}',
                headers={"content-type": "application/json"},
            )
        assert res.status_code == 413, (
            "an oversized body reached the parser (422 would mean it was "
            "read and parsed before being refused)"
        )

    async def test_an_honest_body_is_unaffected(self):
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        ref = await _first_ref(stack, agent_id)
        async with stack.as_machine(agent_id).client() as c:
            res = await c.post(f"/api/operational-agents/{agent_id}/proposals",
                               json=_body(ref, note="a normal note"))
        assert res.status_code == 201

    async def test_other_routes_keep_their_own_limits(self):
        """A ceiling sized for a four-field body must not govern the API."""
        from harkeniq_cc.ingress_body import _is_guarded

        assert _is_guarded({
            "type": "http", "method": "POST",
            "path": "/api/operational-agents/abc/proposals"}) is True
        assert _is_guarded({
            "type": "http", "method": "POST",
            "path": "/api/firmware/cve-feed"}) is False
        assert _is_guarded({
            "type": "http", "method": "GET",
            "path": "/api/operational-agents/abc/proposals"}) is False


class TestExecutionTimeAuthority:
    """A24.15: current authority, not proposal-time authority."""

    async def _approved(self, stack, site_id, agent_id):
        from harkeniq_cc.db.models import CCAgentProposal

        async with stack.sessionmaker() as session:
            row = CCAgentProposal(
                tenant_id=TENANT, agent_id=agent_id,
                actor=f"op-agent:{agent_id}@v1", agent_version=1,
                site_id=site_id, device_agent_id="node-1",
                action_type="IDENTIFY_LED", params={"target": "Disk.Bay.1"},
                rationale="r", evidence={}, disposition="requires_approval",
                authorization_basis="human_approval", status="approved",
                decided_by="owner@example.com", dedupe_key="k-auth",
            )
            session.add(row)
            await session.commit()
            return row.id

    async def test_a_withdrawn_scope_stops_an_approved_proposal(self):
        from harkeniq_cc.agent_runtime import _dispatch_permitted
        from harkeniq_cc.db.models import CCAgentProposal, CCScopeGrant

        stack = await _stack()
        agent_id, site_id = await _ready(stack)
        pid = await self._approved(stack, site_id, agent_id)

        async with stack.sessionmaker() as session:
            proposal = await session.get(CCAgentProposal, pid)
            ok, _ = await _dispatch_permitted(session, TENANT, proposal)
            assert ok is True, "the fixture could not dispatch to begin with"

        # The operator withdraws the agent's reach. E1.2 moved agent scope
        # into the ONE grant table, and A23-3 made withdrawal a revocation
        # rather than a delete, so this is how it really happens.
        await _revoke_agent_scope(stack, agent_id)

        async with stack.sessionmaker() as session:
            proposal = await session.get(CCAgentProposal, pid)
            ok, why = await _dispatch_permitted(session, TENANT, proposal)
        assert ok is False, "a proposal dispatched after its scope was withdrawn"
        assert "no longer reaches" in why

    async def test_a_withdrawn_capability_stops_an_approved_proposal(self):
        from harkeniq_cc.agent_runtime import _dispatch_permitted
        from harkeniq_cc.db.models import CCAgentCapability, CCAgentProposal

        stack = await _stack()
        agent_id, site_id = await _ready(stack)
        pid = await self._approved(stack, site_id, agent_id)

        async with stack.sessionmaker() as session:
            import sqlalchemy as sa
            await session.execute(sa.delete(CCAgentCapability).where(
                CCAgentCapability.agent_id == agent_id,
                CCAgentCapability.capability_ref == "IDENTIFY_LED"))
            await session.commit()

        async with stack.sessionmaker() as session:
            proposal = await session.get(CCAgentProposal, pid)
            ok, why = await _dispatch_permitted(session, TENANT, proposal)
        assert ok is False, "a proposal dispatched for an unbound class"
        assert "no longer bound" in why

    async def test_nothing_was_dispatched_in_either_case(self):
        """The refusal must precede delivery, not follow it."""
        from harkeniq_cc import agent_runtime
        from harkeniq_cc.db.models import CCAgentProposal

        stack = await _stack()
        agent_id, site_id = await _ready(stack)
        await self._approved(stack, site_id, agent_id)
        await _revoke_agent_scope(stack, agent_id)

        dispatched = await agent_runtime.dispatch_decided(stack.state, TENANT)
        assert dispatched == []
        async with stack.sessionmaker() as session:
            import sqlalchemy as sa
            rows = (await session.execute(sa.select(CCAgentProposal))).scalars().all()
        assert all(not r.directive_id for r in rows), "a directive was queued"


class TestTelemetryIsNotTheChain:
    """A24.16: attempt outcomes are counted, not hash-chained."""

    async def test_a_rejected_candidate_does_not_append_to_the_chain(self):
        from harkeniq_cc.db.models import CCAuditLog

        stack = await _stack()
        agent_id, _ = await _ready(stack)
        async with stack.sessionmaker() as session:
            import sqlalchemy as sa
            before = len((await session.execute(sa.select(CCAuditLog))).scalars().all())
        async with stack.as_machine(agent_id).client() as c:
            for n in range(5):
                await c.post(
                    f"/api/operational-agents/{agent_id}/proposals",
                    json=_body("cand_" + "3" * 32, key=f"junk-key-{n:04d}"))
        async with stack.sessionmaker() as session:
            import sqlalchemy as sa
            after = len((await session.execute(sa.select(CCAuditLog))).scalars().all())
        assert after == before, (
            "five rejected submissions appended to the hash chain -- an "
            "amplification channel against the platform's integrity store"
        )

    async def test_a_governed_acceptance_is_still_chained(self):
        from harkeniq_cc.db.models import CCAuditLog

        stack = await _stack()
        agent_id, _ = await _ready(stack)
        ref = await _first_ref(stack, agent_id)
        async with stack.as_machine(agent_id).client() as c:
            await c.post(f"/api/operational-agents/{agent_id}/proposals",
                         json=_body(ref))
        async with stack.sessionmaker() as session:
            import sqlalchemy as sa
            rows = (await session.execute(sa.select(CCAuditLog).where(
                CCAuditLog.action == "agent_submission.accepted"
            ))).scalars().all()
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# 10. Final pre-merge remediation (A24.11-A24.16, round two)
# ---------------------------------------------------------------------------


class TestTheBodyCeilingHoldsForEveryShape:
    """The first implementation held for ONE shape and 400'd for two.

    Measured before the fix: an honest `Content-Length` gave 413, while a
    chunked body and an understated length both gave 400 -- the app saw a
    truncated stream and answered with its own parse failure before the
    413 could be sent. A limit enforced by whoever responds first is not
    enforced.
    """

    @staticmethod
    def _oversized() -> bytes:
        from harkeniq_cc.ingress_body import MAX_INGRESS_BODY_BYTES as LIM

        return (
            b'{"candidate_ref":"cand_x","idempotency_key":"kkkkkkkk","note":"'
            + b"x" * (LIM * 2) + b'"}'
        )

    async def test_an_honest_content_length_is_refused(self):
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        async with stack.as_machine(agent_id).client() as c:
            res = await c.post(
                f"/api/operational-agents/{agent_id}/proposals",
                content=self._oversized(),
                headers={"content-type": "application/json"},
            )
        assert res.status_code == 413

    async def test_a_chunked_body_with_no_declared_length_is_refused(self):
        """The case the first implementation lost: 400, not 413."""
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        payload = self._oversized()

        async def chunks():
            for i in range(0, len(payload), 4096):
                yield payload[i:i + 4096]

        async with stack.as_machine(agent_id).client() as c:
            res = await c.post(
                f"/api/operational-agents/{agent_id}/proposals",
                content=chunks(),
                headers={"content-type": "application/json"},
            )
        assert res.status_code == 413, (
            "a chunked oversized body reached the parser"
        )

    async def test_an_understated_content_length_is_refused(self):
        """The declaration is a hint; the running total is the authority."""
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        async with stack.as_machine(agent_id).client() as c:
            res = await c.post(
                f"/api/operational-agents/{agent_id}/proposals",
                content=self._oversized(),
                headers={
                    "content-type": "application/json",
                    "content-length": "50",
                },
            )
        assert res.status_code == 413

    async def test_a_whitespace_heavy_body_is_refused(self):
        from harkeniq_cc.ingress_body import MAX_INGRESS_BODY_BYTES as LIM

        stack = await _stack()
        agent_id, _ = await _ready(stack)
        payload = (
            b'{"candidate_ref":"cand_x"' + b" " * (LIM * 2)
            + b',"idempotency_key":"kkkkkkkk"}'
        )
        async with stack.as_machine(agent_id).client() as c:
            res = await c.post(
                f"/api/operational-agents/{agent_id}/proposals",
                content=payload,
                headers={"content-type": "application/json"},
            )
        assert res.status_code == 413

    async def test_a_malformed_oversized_body_is_refused_by_size_first(self):
        from harkeniq_cc.ingress_body import MAX_INGRESS_BODY_BYTES as LIM

        stack = await _stack()
        agent_id, _ = await _ready(stack)
        async with stack.as_machine(agent_id).client() as c:
            res = await c.post(
                f"/api/operational-agents/{agent_id}/proposals",
                content=b"{" * (LIM * 2),
                headers={"content-type": "application/json"},
            )
        assert res.status_code == 413

    async def test_a_chunked_body_within_the_limit_is_replayed_intact(self):
        """Buffering must not change what the application receives."""
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        ref = await _first_ref(stack, agent_id)
        payload = (
            '{"candidate_ref":"%s","idempotency_key":"chunk-key-01"}' % ref
        ).encode()

        async def chunks():
            for i in range(0, len(payload), 8):
                yield payload[i:i + 8]

        async with stack.as_machine(agent_id).client() as c:
            res = await c.post(
                f"/api/operational-agents/{agent_id}/proposals",
                content=chunks(),
                headers={"content-type": "application/json"},
            )
        assert res.status_code == 201, res.text

    async def test_the_ceiling_does_not_shadow_schema_validation(self):
        """A legal-sized body with an illegal field is still a 422."""
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        ref = await _first_ref(stack, agent_id)
        async with stack.as_machine(agent_id).client() as c:
            res = await c.post(
                f"/api/operational-agents/{agent_id}/proposals",
                json=_body(ref, note="y" * 4000),
            )
        assert res.status_code == 422

    async def test_nothing_reached_the_application_for_an_oversized_body(self):
        """413 must cost nothing: no attempt, no submission, no proposal."""
        import sqlalchemy as sa

        from harkeniq_cc.db.models import CCAgentIngressAttempt

        stack = await _stack()
        agent_id, _ = await _ready(stack)
        async with stack.as_machine(agent_id).client() as c:
            await c.post(
                f"/api/operational-agents/{agent_id}/proposals",
                content=self._oversized(),
                headers={"content-type": "application/json"},
            )
        async with stack.sessionmaker() as session:
            assert (await session.execute(
                sa.select(CCAgentIngressAttempt))).scalars().all() == []
            assert (await session.execute(
                sa.select(CCAgentSubmission))).scalars().all() == []


class TestAuthenticatedRefusalsAreMetered:
    """A24.13: a valid credential must not buy unlimited free refusals."""

    @staticmethod
    async def _attempts(stack, agent_id) -> list:
        import sqlalchemy as sa

        from harkeniq_cc.db.models import CCAgentIngressAttempt

        async with stack.sessionmaker() as session:
            return (await session.execute(
                sa.select(CCAgentIngressAttempt).where(
                    CCAgentIngressAttempt.agent_id == agent_id)
            )).scalars().all()

    async def test_a_permission_refusal_is_metered(self):
        """403 for a missing permission still costs the caller."""
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        # Authenticated, but without proposal.submit.
        stack.as_machine(agent_id, permissions=("fleet.view",))
        async with stack.client() as c:
            res = await c.post(
                f"/api/operational-agents/{agent_id}/proposals",
                json=_body("cand_" + "0" * 32),
            )
        assert res.status_code == 403
        assert "proposal.submit" in res.json()["detail"]
        rows = await self._attempts(stack, agent_id)
        assert len(rows) == 1 and rows[0].outcome == "refused"

    async def test_an_impersonation_refusal_is_metered_to_the_caller(self):
        """And charged to the TOKEN's agent, not the one it named."""
        stack = await _stack()
        agent_id, site_id = await _ready(stack)
        async with stack.client() as c:
            other = await _agent(c, site_id, name="Third Shift")
        async with stack.as_machine(agent_id).client() as c:
            res = await c.post(
                f"/api/operational-agents/{other}/proposals",
                json=_body("cand_" + "0" * 32),
            )
        assert res.status_code == 403
        assert len(await self._attempts(stack, agent_id)) == 1, (
            "the caller was not charged for its own impersonation attempt"
        )
        assert await self._attempts(stack, other) == [], (
            "the impersonated agent was charged for someone else's attempt"
        )

    async def test_a_scope_refusal_is_metered(self):
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        await _revoke_agent_scope(stack, agent_id)
        async with stack.as_machine(agent_id).client() as c:
            res = await c.post(
                f"/api/operational-agents/{agent_id}/proposals",
                json=_body("cand_" + "0" * 32),
            )
        assert res.status_code == 404
        rows = await self._attempts(stack, agent_id)
        assert len(rows) == 1 and rows[0].outcome == "refused"

    @pytest.mark.parametrize("status", ["draft", "paused", "retired"])
    async def test_a_lifecycle_refusal_is_metered(self, status):
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        async with stack.sessionmaker() as session:
            agent = await session.get(CCOperationalAgent, agent_id)
            agent.status = status
            await session.commit()
        async with stack.as_machine(agent_id).client() as c:
            res = await c.post(
                f"/api/operational-agents/{agent_id}/proposals",
                json=_body("cand_" + "0" * 32),
            )
        assert res.status_code == 409
        rows = await self._attempts(stack, agent_id)
        assert len(rows) == 1 and rows[0].outcome == "refused"

    async def test_repeated_refusals_exhaust_the_window(self):
        """The point of metering them: refusals are finite."""
        from harkeniq_cc.db.repos import AgentIngressAttemptRepo
        from harkeniq_cc.ingress_limits import ATTEMPT_MAX

        stack = await _stack()
        agent_id, _ = await _ready(stack)
        async with stack.sessionmaker() as session:
            repo = AgentIngressAttemptRepo(session)
            for _ in range(ATTEMPT_MAX):
                await repo.record(
                    tenant_id=TENANT, agent_id=agent_id, outcome="refused")
            await session.commit()
        # Even a permission refusal now costs nothing MORE -- it is 429.
        stack.as_machine(agent_id, permissions=("fleet.view",))
        async with stack.client() as c:
            res = await c.post(
                f"/api/operational-agents/{agent_id}/proposals",
                json=_body("cand_" + "0" * 32),
            )
        assert res.status_code == 429

    async def test_a_human_is_refused_without_a_meter_to_charge(self):
        """A non-machine principal has no per-agent bucket to bill."""
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        stack.as_person(role="platform_super_admin")
        async with stack.client() as c:
            res = await c.post(
                f"/api/operational-agents/{agent_id}/proposals",
                json=_body("cand_" + "0" * 32),
            )
        assert res.status_code == 403
        assert "machine-principal surface" in res.json()["detail"]
        assert await self._attempts(stack, agent_id) == []


class TestOnlyInFlightWorkFences:
    """A24.12: a settled operation must not fence a device forever."""

    def test_the_in_flight_set_is_not_the_open_set(self):
        from harkeniq_cc.db.repos import AgentProposalRepo as R

        assert "denied" in R.OPEN_STATUSES, (
            "denial must still block the PER-AGENT dedupe rule (D16)"
        )
        assert "denied" not in R.IN_FLIGHT_STATUSES, (
            "a denied proposal can never reach a device; fencing on it "
            "would let one refusal block that operation for every agent"
        )

    def test_every_in_flight_status_can_still_reach_a_device(self):
        from harkeniq_cc.db.repos import AgentProposalRepo as R

        assert set(R.IN_FLIGHT_STATUSES) == {
            "proposed", "awaiting_approval", "approved", "dispatched",
        }

    @pytest.mark.parametrize("status,fences", [
        ("proposed", True),
        ("awaiting_approval", True),
        ("approved", True),
        ("dispatched", True),
        ("denied", False),
        ("completed", False),
        ("failed", False),
        ("blocked", False),
    ])
    async def test_the_lifecycle_decides_what_fences(self, status, fences):
        from harkeniq_cc.db.models import CCAgentProposal
        from harkeniq_cc.proposal_admission import (
            CODE_OPERATION_IN_FLIGHT, admit_proposal,
        )

        stack = await _stack()
        agent_a, site_id = await _ready(stack)
        async with stack.client() as c:
            agent_b = await _agent(c, site_id, name=f"Shift {status}")

        def payload(agent_id, key):
            return {
                "tenant_id": TENANT, "agent_id": agent_id,
                "actor": f"op-agent:{agent_id}@v1", "agent_version": 1,
                "site_id": site_id, "device_agent_id": "node-1",
                "action_type": "IDENTIFY_LED",
                "params": {"target": "Disk.Bay.1"}, "rationale": "r",
                "evidence": {}, "disposition": "requires_approval",
                "disposition_reason": "", "blocking_conditions": [],
                "authorization_basis": "human_approval",
                "status": "awaiting_approval", "decided_by": "",
                "decided_at": None, "dedupe_key": key,
            }

        async with stack.sessionmaker() as session:
            first, _, _ = await admit_proposal(
                session, tenant_id=TENANT, payload=payload(agent_a, "k-a"),
                origin="ingress")
            await session.commit()
            first_id = first.id
        async with stack.sessionmaker() as session:
            row = await session.get(CCAgentProposal, first_id)
            row.status = status
            await session.commit()

        async with stack.sessionmaker() as session:
            second, code, _ = await admit_proposal(
                session, tenant_id=TENANT, payload=payload(agent_b, "k-b"),
                origin="ingress")

        if fences:
            assert second is None and code == CODE_OPERATION_IN_FLIGHT, (
                f"status {status!r} should fence an equivalent operation"
            )
        else:
            assert second is not None, (
                f"status {status!r} fenced an equivalent operation, but work "
                f"in that state can never reach a device"
            )

    async def test_a_denied_operation_does_not_block_another_agent(self):
        """The headline of MEDIUM 1, stated as the product rule."""
        from harkeniq_cc.db.models import CCAgentProposal
        from harkeniq_cc.proposal_admission import admit_proposal

        stack = await _stack()
        agent_a, site_id = await _ready(stack)
        async with stack.client() as c:
            agent_b = await _agent(c, site_id, name="Later Shift")

        def payload(agent_id, key):
            return {
                "tenant_id": TENANT, "agent_id": agent_id,
                "actor": f"op-agent:{agent_id}@v1", "agent_version": 1,
                "site_id": site_id, "device_agent_id": "node-1",
                "action_type": "IDENTIFY_LED",
                "params": {"target": "Disk.Bay.1"}, "rationale": "r",
                "evidence": {}, "disposition": "requires_approval",
                "disposition_reason": "", "blocking_conditions": [],
                "authorization_basis": "human_approval",
                "status": "awaiting_approval", "decided_by": "",
                "decided_at": None, "dedupe_key": key,
            }

        async with stack.sessionmaker() as session:
            row, _, _ = await admit_proposal(
                session, tenant_id=TENANT, payload=payload(agent_a, "d-a"),
                origin="ingress")
            await session.commit()
            pid = row.id
        async with stack.sessionmaker() as session:
            denied = await session.get(CCAgentProposal, pid)
            denied.status = "denied"
            await session.commit()
        async with stack.sessionmaker() as session:
            second, code, _ = await admit_proposal(
                session, tenant_id=TENANT, payload=payload(agent_b, "d-b"),
                origin="ingress")
        assert second is not None, (
            f"one human's denial fenced every other agent forever ({code})"
        )
