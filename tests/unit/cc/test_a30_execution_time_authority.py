"""A6-4B0a remediation (A30.17): approval is not perpetual execution authority.

THE DEFECT, AS REPRODUCED BY INDEPENDENT REVIEW
-----------------------------------------------
Central Command had TWO places that decided whether an approved
Operational Agent proposal may cross the CC -> SM boundary, and they did
not ask the same questions:

    gate                    sync human approval     background runtime
    current scope           NEVER ASKED             site_ids only
    capability still bound  NEVER ASKED             asked
    tenant stop switch      asked                   NEVER ASKED

So on the synchronous path an ACTIVE, an EXPIRED and a REVOKED grant all
produced HTTP 200, an SM call and a dispatched directive, while the
background path correctly refused the last two. A slice whose objective
is "expired means expired everywhere operational reach is evaluated"
cannot ship while one production dispatch path executes after expiry.

WHAT THIS MODULE HOLDS
----------------------
* both paths consume ONE gate (`agent_runtime.revalidate_dispatch`) and
  that gate is the only caller of the `dispatch_permitted` algebra
  (structural)
* the lifecycle matrix -- unchanged, expired, revoked, deactivated, stop
  switch, target left scope -- refuses identically on BOTH paths, and the
  refusal precedes the SM call
* the target is checked, not "the agent has some scope": a surviving
  grant elsewhere does not carry an approved target that left scope
* the approval began under live authority and completed after expiry
  (dual approval) does not dispatch
* historical proposal and approval records survive a refusal unchanged
* the evaluator lifecycle proof is NON-vacuous: the same configuration
  proposes while the grant is live and proposes nothing once it lapses

What this does NOT claim: the gate is evaluated in Central Command's
transaction immediately before the SM call. A grant that expires in the
milliseconds between that read and the Site Manager queueing the
directive is not caught here -- CC takes no lock on grant rows, and the
Site Manager does not know CC scope. The Site Manager's lease,
preconditions and blast radius and the node's own allow list remain the
final execution authority, unchanged.
"""

from __future__ import annotations

import ast
import pathlib
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import sqlalchemy as sa

from harkeniq_cc import agent_runtime
from harkeniq_cc.api.deps import get_current_user
from harkeniq_cc.app import create_app
from harkeniq_cc.auth import ROLE_PERMISSIONS, UserContext, configure_auth
from harkeniq_cc.config import CCConfig
from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_cc.db.models import (
    CCAgentProposal,
    CCApprovalRecord,
    CCAuditLog,
    CCFleetCache,
    CCIncident,
    CCOperationalAgent,
    CCScopeGrant,
    CCSite,
)
from harkeniq_cc.runtime import AppState

from tests.unit.cc.conftest import seed_tenant_people

TENANT = "t1"
OWNER = ("kc-owner", "owner@example.com", "tenant_owner")
SECOND = ("kc-second", "second@example.com", "tenant_owner")

CC_SRC = pathlib.Path(__file__).resolve().parents[3] / (
    "services/central_command/src/harkeniq_cc"
)


# ---------------------------------------------------------------------------
# The Site Manager, observed
# ---------------------------------------------------------------------------


class FakeSM:
    """Records every CC -> SM dispatch. `raise_` simulates an unreachable
    site, which is how a human-approved proposal is left `approved` for
    the background pass to deliver."""

    calls: list = []
    raise_: bool = False

    def __init__(self, *_a, **_kw):
        pass

    async def dispatch_action(self, endpoint, token, **kw):
        if FakeSM.raise_:
            raise ConnectionError("site manager unreachable")
        FakeSM.calls.append(kw)
        return {"accepted": True, "directive_id": f"d{len(FakeSM.calls)}",
                "reason": ""}


@pytest.fixture(autouse=True)
def _fake_sm(monkeypatch):
    FakeSM.calls, FakeSM.raise_ = [], False
    monkeypatch.setattr("harkeniq_cc.agent_runtime.SMClient", FakeSM)
    monkeypatch.setattr("harkeniq_cc.api.approvals.SMClient", FakeSM)
    yield


# ---------------------------------------------------------------------------
# A real stack: real routes, real repositories, real resolver
# ---------------------------------------------------------------------------


class Stack:
    def __init__(self, app, state):
        self.app, self.state = app, state
        self.sessionmaker = state.sessionmaker
        self.persona = OWNER

    def as_person(self, persona):
        self.persona = persona
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
    await seed_tenant_people(
        state.sessionmaker, TENANT,
        [(OWNER[0], OWNER[2]), (SECOND[0], SECOND[2])],
    )

    async def _fake():
        sub, email, role = stack.persona
        return UserContext(
            user_id=sub, email=email, tenant_id=TENANT, role=role,
            permissions=list(ROLE_PERMISSIONS[role]),
        )

    app.dependency_overrides[get_current_user] = _fake
    return stack


CAPS = {
    "reach_known": True,
    "implemented": ["IDENTIFY_LED", "COLLECT_DIAGNOSTICS"],
    "allowed": ["IDENTIFY_LED", "COLLECT_DIAGNOSTICS"],
    "effective": ["IDENTIFY_LED", "COLLECT_DIAGNOSTICS"],
}


async def _seed_estate(stack) -> tuple[str, str]:
    """Two sites. node-1 at A has an open disk incident; node-2 sits at B."""
    async with stack.sessionmaker() as session:
        site_a = CCSite(tenant_id=TENANT, site_name="DC-A",
                        sm_endpoint="sm-a:50051", sm_token="tok-a")
        site_b = CCSite(tenant_id=TENANT, site_name="DC-B",
                        sm_endpoint="sm-b:50051", sm_token="tok-b")
        session.add_all([site_a, site_b])
        await session.flush()
        session.add_all([
            CCFleetCache(
                site_id=site_a.id, agent_id="node-1", agent_name="n1",
                vendor="Dell", model="R750", device_class="server",
                observation="observed", health="Critical", capabilities=CAPS,
            ),
            CCFleetCache(
                site_id=site_b.id, agent_id="node-2", agent_name="n2",
                vendor="Dell", model="R750", device_class="server",
                observation="observed", health="OK", capabilities=CAPS,
            ),
            CCIncident(
                incident_id="inc-1", tenant_id=TENANT, site_id=site_a.id,
                kind="device", status="open", title="disk failing on n1",
                device_agent_id="node-1", subsystem="disk", confidence=0.9,
                components=[{"component": "Disk.Bay.1", "severity": "CRITICAL"}],
            ),
        ])
        await session.commit()
        return site_a.id, site_b.id


async def _agent(stack, scopes) -> str:
    """An agent created through the real API, bound to IDENTIFY_LED, and
    switched on in the state a real activation leaves it in."""
    async with stack.as_person(OWNER).client() as c:
        res = await c.post("/api/operational-agents/", json={
            "name": "dispatch-probe",
            "scopes": scopes,
            "capabilities": [
                {"kind": "action_class", "capability_ref": "IDENTIFY_LED"},
            ],
        })
    assert res.status_code == 201, res.text
    agent_id = res.json()["id"]
    async with stack.sessionmaker() as session:
        agent = await session.get(CCOperationalAgent, agent_id)
        agent.status = "active"
        agent.activated_version = agent.version
        await session.commit()
    return agent_id


async def _admit(stack) -> CCAgentProposal:
    """Admission through the ONE admission path, under live authority."""
    created = await agent_runtime.evaluate_agents(stack.state, TENANT)
    assert len(created) == 1, (
        f"the fixture must admit exactly one proposal, got {len(created)}"
    )
    proposal = created[0]
    assert proposal.status == "awaiting_approval", proposal.status
    assert proposal.device_agent_id == "node-1"
    return proposal


async def _proposal(stack, pid) -> CCAgentProposal:
    async with stack.sessionmaker() as session:
        return await session.get(CCAgentProposal, pid)


# -- the authority changes, applied the way an operator applies them ------


async def _grants(session, agent_id, scope_ref=None):
    stmt = sa.select(CCScopeGrant).where(
        CCScopeGrant.principal_type == "agent",
        CCScopeGrant.principal_ref == agent_id,
    )
    if scope_ref is not None:
        stmt = stmt.where(CCScopeGrant.scope_ref == scope_ref)
    return (await session.execute(stmt)).scalars().all()


async def _expire(stack, agent_id, scope_ref=None):
    async with stack.sessionmaker() as session:
        for g in await _grants(session, agent_id, scope_ref):
            g.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await session.commit()


async def _revoke(stack, agent_id, scope_ref=None):
    async with stack.sessionmaker() as session:
        for g in await _grants(session, agent_id, scope_ref):
            g.revoked_at = datetime.now(timezone.utc)
            g.revoked_by = "kc-owner"
        await session.commit()


async def _deactivate(stack, agent_id):
    async with stack.sessionmaker() as session:
        agent = await session.get(CCOperationalAgent, agent_id)
        agent.status = "paused"
        await session.commit()


async def _stop(stack):
    from harkeniq_cc.db.repos import StopSwitchRepo

    async with stack.sessionmaker() as session:
        await StopSwitchRepo(session).set(TENANT, True, "kc-owner")
        await session.commit()


# -- the two production dispatch paths ------------------------------------


async def _dispatch_sync(stack, pid, change):
    """Path A: a named human approves; CC dispatches in the request."""
    await change()
    async with stack.as_person(OWNER).client() as c:
        res = await c.post(f"/api/approvals/{pid}/approve")
    assert res.status_code == 200, res.text
    return res.json()


async def _dispatch_background(stack, pid, change):
    """Path B: the approval is recorded while authority is LIVE, the site
    is briefly unreachable so the proposal stays `approved`, authority
    changes, and the background pass is what tries to deliver it."""
    FakeSM.raise_ = True
    async with stack.as_person(OWNER).client() as c:
        res = await c.post(f"/api/approvals/{pid}/approve")
    assert res.status_code == 200, res.text
    assert res.json()["delivery"]["delivered"] is False
    assert (await _proposal(stack, pid)).status == "approved"
    FakeSM.raise_ = False
    await change()
    return await agent_runtime.dispatch_decided(stack.state, TENANT)


PATHS = ("sync", "background")


async def _run(stack, path, pid, change):
    if path == "sync":
        return await _dispatch_sync(stack, pid, change)
    return await _dispatch_background(stack, pid, change)


def _refusal_reason(path, result, proposal) -> str:
    if path == "sync":
        return result["delivery"]["reason"]
    return proposal.dispatch_reason or ""


# ---------------------------------------------------------------------------
# 1. The matrix, on BOTH paths
# ---------------------------------------------------------------------------


CHANGES = {
    # name: (apply(stack, agent_id, site_a, site_b), refusal substring)
    "expired": (lambda s, a, sa_, sb: _expire(s, a), "no longer reaches"),
    "revoked": (lambda s, a, sa_, sb: _revoke(s, a), "no longer reaches"),
    "deactivated": (lambda s, a, sa_, sb: _deactivate(s, a), "not active"),
    "stop_switch": (lambda s, a, sa_, sb: _stop(s), "stop switch"),
    # The approved target's site grant lapses; the site-B grant SURVIVES.
    # "The agent still has some scope" must not carry node-1.
    "target_left_scope": (
        lambda s, a, sa_, sb: _expire(s, a, scope_ref=sa_), "no longer reaches",
    ),
}


class TestTheMatrixOnBothPaths:
    @pytest.mark.parametrize("path", PATHS)
    async def test_unchanged_authority_dispatches(self, path):
        stack = await _stack()
        site_a, site_b = await _seed_estate(stack)
        await _agent(stack, [
            {"scope_type": "site", "scope_ref": site_a},
            {"scope_type": "site", "scope_ref": site_b},
        ])
        proposal = await _admit(stack)

        async def _noop():
            return None

        await _run(stack, path, proposal.id, _noop)

        assert len(FakeSM.calls) == 1, "live authority must still dispatch"
        call = FakeSM.calls[0]
        assert call["device_agent_id"] == "node-1"
        assert call["proposal_id"] == proposal.id
        assert (await _proposal(stack, proposal.id)).status == "dispatched"

    @pytest.mark.parametrize("path", PATHS)
    @pytest.mark.parametrize("change", sorted(CHANGES))
    async def test_changed_authority_never_crosses_cc_to_sm(self, path, change):
        stack = await _stack()
        site_a, site_b = await _seed_estate(stack)
        agent_id = await _agent(stack, [
            {"scope_type": "site", "scope_ref": site_a},
            {"scope_type": "site", "scope_ref": site_b},
        ])
        proposal = await _admit(stack)
        apply, expected = CHANGES[change]

        result = await _run(
            stack, path, proposal.id,
            lambda: apply(stack, agent_id, site_a, site_b),
        )

        assert FakeSM.calls == [], (
            f"{path}: an approved proposal crossed CC -> SM after "
            f"'{change}' -- approval is not perpetual execution authority"
        )
        after = await _proposal(stack, proposal.id)
        assert after.status != "dispatched"
        assert not after.directive_id
        reason = _refusal_reason(path, result, after)
        assert expected in reason, (path, change, reason)


# ---------------------------------------------------------------------------
# 2. TOCTOU: authority that was live when approval BEGAN
# ---------------------------------------------------------------------------


class TestAuthorityThatLapsesMidDecision:
    async def test_dual_approval_completed_after_expiry_does_not_dispatch(self):
        """Approval began under live authority and completed after it lapsed.

        The first approver recorded while the grant was live; the grant
        expired; the second approver completed the policy. Completion is
        an approval fact -- it is recorded -- and it is not execution
        authority.
        """
        stack = await _stack()
        site_a, _site_b = await _seed_estate(stack)
        agent_id = await _agent(stack, [{"scope_type": "site", "scope_ref": site_a}])
        proposal = await _admit(stack)

        async with stack.as_person(OWNER).client() as c:
            res = await c.post("/api/policies/", json={
                "name": "dual", "action_type": "*", "device_type": "*",
                "risk_level": "*", "required_approvers": 2,
            })
            assert res.status_code == 200, res.text
            first = await c.post(f"/api/approvals/{proposal.id}/approve")
            assert first.json()["recorded"] is True
            assert first.json()["approval"]["remaining"] == 1

        await _expire(stack, agent_id)

        async with stack.as_person(SECOND).client() as c:
            second = await c.post(f"/api/approvals/{proposal.id}/approve")
        assert second.status_code == 200, second.text
        body = second.json()
        assert body["decision"] == "approved", "the approval itself stands"
        assert body["delivery"]["delivered"] is False
        assert "no longer reaches" in body["delivery"]["reason"]
        assert FakeSM.calls == []

        async with stack.sessionmaker() as session:
            records = (await session.execute(
                sa.select(CCApprovalRecord).where(
                    CCApprovalRecord.subject_ref == proposal.id)
            )).scalars().all()
        assert sorted(r.approver_ref for r in records) == ["kc-owner", "kc-second"]


# ---------------------------------------------------------------------------
# 3. Target-specific: the canonical reach answer, not the site_ids projection
# ---------------------------------------------------------------------------


class TestTheTargetIsWhatIsChecked:
    @pytest.mark.parametrize("path", PATHS)
    @pytest.mark.parametrize("scope_type,ref", [
        ("device", "node-1"),
        ("device_class", "server"),
    ])
    async def test_device_reach_is_judged_by_the_admission_answer(
        self, path, scope_type, ref,
    ):
        """Dispatch asks the SAME reach function admission asked.

        A device or device_class grant is admitted by `load_agent_reach`
        (type-complete over effective grants). Judging dispatch by the
        `site_ids` projection instead would refuse at dispatch what
        admission admitted -- a second, disagreeing answer (A30.3:
        `site_ids` is a convenience projection, never a reach answer).
        F1 -- the repository READ filter built from `site_ids` -- is not
        touched by this and stays open.
        """
        stack = await _stack()
        await _seed_estate(stack)
        await _agent(stack, [{"scope_type": scope_type, "scope_ref": ref}])
        proposal = await _admit(stack)

        async def _noop():
            return None

        await _run(stack, path, proposal.id, _noop)
        assert len(FakeSM.calls) == 1

    @pytest.mark.parametrize("path", PATHS)
    @pytest.mark.parametrize("scope_type,ref", [
        ("device", "node-1"),
        ("device_class", "server"),
    ])
    async def test_expired_device_reach_does_not_dispatch(
        self, path, scope_type, ref,
    ):
        stack = await _stack()
        await _seed_estate(stack)
        agent_id = await _agent(stack, [{"scope_type": scope_type, "scope_ref": ref}])
        proposal = await _admit(stack)

        await _run(stack, path, proposal.id, lambda: _expire(stack, agent_id))
        assert FakeSM.calls == []

    @pytest.mark.parametrize("path", PATHS)
    async def test_a_surviving_device_grant_elsewhere_carries_nothing(self, path):
        """node-1's grant lapses; a live grant on node-2 remains."""
        stack = await _stack()
        await _seed_estate(stack)
        agent_id = await _agent(stack, [
            {"scope_type": "device", "scope_ref": "node-1"},
            {"scope_type": "device", "scope_ref": "node-2"},
        ])
        proposal = await _admit(stack)

        await _run(
            stack, path, proposal.id,
            lambda: _revoke(stack, agent_id, scope_ref="node-1"),
        )
        assert FakeSM.calls == []

    async def test_a_target_missing_from_the_fleet_is_not_in_reach(self):
        """Reach is computed over the fleet, at admission and at dispatch.
        A device that left the site's fleet cannot be placed in scope."""
        stack = await _stack()
        site_a, _ = await _seed_estate(stack)
        await _agent(stack, [{"scope_type": "site", "scope_ref": site_a}])
        proposal = await _admit(stack)

        async def _gone():
            async with stack.sessionmaker() as session:
                await session.execute(sa.delete(CCFleetCache).where(
                    CCFleetCache.agent_id == "node-1"))
                await session.commit()

        result = await _dispatch_sync(stack, proposal.id, _gone)
        assert FakeSM.calls == []
        assert "no longer reaches" in result["delivery"]["reason"]


# ---------------------------------------------------------------------------
# 4. History is not rewritten by a refusal
# ---------------------------------------------------------------------------


class TestHistoryIsPreserved:
    @pytest.mark.parametrize("path", PATHS)
    async def test_proposal_and_approval_records_survive_a_refusal(self, path):
        stack = await _stack()
        site_a, _ = await _seed_estate(stack)
        agent_id = await _agent(stack, [{"scope_type": "site", "scope_ref": site_a}])
        proposal = await _admit(stack)
        admitted = {
            "actor": proposal.actor,
            "agent_version": proposal.agent_version,
            "action_type": proposal.action_type,
            "device_agent_id": proposal.device_agent_id,
            "site_id": proposal.site_id,
            "params": proposal.params,
            "dedupe_key": proposal.dedupe_key,
            "provenance_type": proposal.provenance_type,
        }

        await _run(stack, path, proposal.id, lambda: _expire(stack, agent_id))
        assert FakeSM.calls == []

        after = await _proposal(stack, proposal.id)
        for field, value in admitted.items():
            assert getattr(after, field) == value, f"{field} was rewritten"
        # The named human who approved it is still on the record, and the
        # time they did. A lifecycle refusal is not a second decision.
        assert after.decided_by == OWNER[1]
        assert after.decided_at is not None
        assert after.authorization_basis == "human_approval"

        async with stack.sessionmaker() as session:
            records = (await session.execute(
                sa.select(CCApprovalRecord).where(
                    CCApprovalRecord.subject_ref == proposal.id)
            )).scalars().all()
            actions = [
                r.action for r in (await session.execute(
                    sa.select(CCAuditLog).where(
                        CCAuditLog.subject == proposal.id)
                    .order_by(CCAuditLog.seq)
                )).scalars().all()
            ]
        assert len(records) == 1, "exactly the one human decision"
        assert records[0].decision == "approved"
        assert records[0].approver_ref == OWNER[0]
        # The E0.1 per-approver entry is the decision on the chain; it is
        # written before the gate runs, so a refusal cannot unwrite it.
        assert actions.count("approval.approved") == 1, actions
        refusal = (
            "agent_proposal.refused_at_dispatch" if path == "sync"
            else "agent_proposal.dispatch_withheld"
        )
        assert refusal in actions, actions
        assert "agent_proposal.dispatched" not in actions


# ---------------------------------------------------------------------------
# 5. The evaluator lifecycle proof, made non-vacuous
# ---------------------------------------------------------------------------


class TestTheEvaluatorProofIsNotVacuous:
    """The earlier proof seeded no capability and no incident, so ACTIVE
    and EXPIRED both produced zero proposals and it could not fail. Here
    the SAME configuration is shown able to propose, then shown to stop."""

    async def test_same_configuration_proposes_live_and_stops_expired(self):
        stack = await _stack()
        site_a, _ = await _seed_estate(stack)
        agent_id = await _agent(stack, [{"scope_type": "site", "scope_ref": site_a}])

        # LIVE, read-only first: the configuration would propose.
        async with stack.as_person(OWNER).client() as c:
            res = await c.get(f"/api/operational-agents/{agent_id}/dry-run")
        assert res.status_code == 200, res.text
        assert [w["device_agent_id"] for w in res.json()["would_propose"]] == [
            "node-1"
        ], "the fixture must be capable of proposing, or the proof is vacuous"

        # EXPIRED: the same configuration reaches nothing, on every reader.
        await _expire(stack, agent_id)
        async with stack.as_person(OWNER).client() as c:
            dry = await c.get(f"/api/operational-agents/{agent_id}/dry-run")
            runtime = await c.get(f"/api/operational-agents/{agent_id}/runtime")
        assert dry.json()["would_propose"] == []
        assert runtime.json()["devices"]["in_scope"] == 0
        assert await agent_runtime.evaluate_agents(stack.state, TENANT) == []

        # RENEWED: the same configuration proposes again -- so the empty
        # answer above was the lifecycle, not the fixture.
        async with stack.sessionmaker() as session:
            for g in await _grants(session, agent_id):
                g.expires_at = datetime.now(timezone.utc) + timedelta(days=30)
            await session.commit()
        created = await agent_runtime.evaluate_agents(stack.state, TENANT)
        assert [p.device_agent_id for p in created] == ["node-1"]


# ---------------------------------------------------------------------------
# 6. Structural: one gate, both paths
# ---------------------------------------------------------------------------


def _calls_by_function(name: str) -> set[str]:
    found: set[str] = set()
    for path in CC_SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                f = call.func
                called = (
                    f.attr if isinstance(f, ast.Attribute)
                    else f.id if isinstance(f, ast.Name) else ""
                )
                if called == name:
                    found.add(f"{path.name}:{node.name}")
    return found


class TestOneGate:
    def test_both_dispatch_paths_call_the_one_gate(self):
        assert _calls_by_function("revalidate_dispatch") == {
            "agent_runtime.py:dispatch_decided",
            "approvals.py:_decide_agent_proposal",
        }

    def test_the_gate_algebra_has_exactly_one_production_caller(self):
        """`dispatch_permitted` is fail-closed over DISPATCH_GATES; a
        second caller assembling its own inputs is how the two paths came
        to disagree."""
        assert _calls_by_function("dispatch_permitted") == {
            "agent_runtime.py:revalidate_dispatch",
        }

    def test_every_agent_proposal_dispatch_is_gated(self):
        """Every function that hands work to the SM verb either consumes
        the gate or is the campaign runner, whose work is governed by
        its own plan-bound approval (A18) and is not an agent proposal."""
        senders = _calls_by_function("dispatch_action") - {
            # the client's own definition calls the stub, not itself
            "sm_client.py:dispatch_action",
        }
        gated = _calls_by_function("revalidate_dispatch")
        assert senders - gated == {"campaign_runner.py:dispatch_wave"}, (
            f"ungated CC -> SM dispatch: {sorted(senders - gated)}"
        )

    def test_the_two_divergent_gates_are_gone(self):
        from harkeniq_cc.api import approvals

        assert not hasattr(approvals, "_agent_dispatch_gates")
        assert not hasattr(agent_runtime, "_dispatch_permitted")

    def test_reach_and_binding_are_gates_in_the_algebra(self):
        from harkeniq_cc.agent_activation import DISPATCH_GATES

        for gate in ("stop_switch", "effective_scope", "capability_binding"):
            assert gate in DISPATCH_GATES
