"""A6-2: the machine lifecycle receipt (A25.2–A25.7).

Three properties carry this module, and each has a failure mode that
would look fine from the outside:

* **A submission id is not a bearer credential.** Possession must prove
  nothing. Agent A holding B's id gets 404 — not 403, because here the
  identifier is the thing being guessed and confirming it exists is the
  leak.
* **The historical exception narrows, it does not open.** An agent whose
  scope was revoked may close its own transaction and learn NOTHING about
  the estate. Every withheld field is asserted by name; a projection that
  filtered by exclusion would leak the next field somebody adds upstream.
* **The layers stay six.** `approved` is not terminal, and `PARTIAL` is
  not a plain failure. Both would be invisible in a flattened status.
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
    CCAgentProposal, CCAgentSubmission, CCFleetCache, CCOutcomeHistory, CCSite,
)
from harkeniq_cc.runtime import AppState
from harkeniq_cc.route_contract import MACHINE_JOBS

from tests.unit.cc.conftest import seed_tenant_admin

TENANT = "t1"


class Stack:
    def __init__(self, app, state):
        self.app, self.state = app, state
        self.sessionmaker = state.sessionmaker
        self.persona = ("kc-owner", "owner@example.com", "tenant_owner")
        self.machine = None
        self.tenant_wide = True
        self.site_ids: set = set()

    def as_machine(self, agent_id, permissions=("fleet.view", "incident.view"),
                   jobs=None):
        """A29.6: `jobs` are the agent's A0 BINDINGS, as production carries
        them. Defaults to the full declared set -- these suites test what a
        machine may do GIVEN the bindings; the A29 suite passes a narrower
        set to prove the binding gate itself."""
        self.machine = (agent_id, list(permissions))
        self.machine_jobs = MACHINE_JOBS if jobs is None else frozenset(jobs)
        return self

    def as_person(self, role="tenant_owner"):
        self.persona, self.machine = ("kc-owner", "owner@example.com", role), None
        return self

    def narrow_to(self, site_ids):
        """Withdraw tenant-wide reach, leaving only these sites."""
        self.tenant_wide, self.site_ids = False, set(site_ids)
        return self

    def client(self):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://t",
        )


async def _stack():
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

    async def _fake_user():
        if stack.machine is not None:
            agent_id, perms = stack.machine
            return UserContext(
                user_id=agent_id, email=f"op-agent:{agent_id}@v1",
                tenant_id=TENANT, role="", permissions=perms,
                species="agent", identity_id="id-1",
                machine_jobs=getattr(stack, "machine_jobs", MACHINE_JOBS),
            )
        sub, email, role = stack.persona
        return UserContext(
            user_id=sub, email=email, tenant_id=TENANT, role=role,
            permissions=list(ROLE_PERMISSIONS[role]),
        )

    async def _fake_scope():
        class S:
            tenant_wide = True
            site_ids: set = set()
        s = S()
        s.tenant_wide = stack.tenant_wide
        s.site_ids = stack.site_ids
        return s

    from harkeniq_cc.api.deps import get_scope

    app.dependency_overrides[get_current_user] = _fake_user
    app.dependency_overrides[get_scope] = _fake_scope
    return stack


async def _agent_row(stack, agent_id="agent-A", site_id=""):
    """A real operational agent row.

    The proposal fixtures reference an agent id; the routes that resolve
    the AGENT (the proposal list) need the row to exist, and a fixture
    that skipped it was testing a 404 rather than the projection.
    """
    from harkeniq_cc.db.models import CCOperationalAgent, CCScopeGrant

    async with stack.sessionmaker() as session:
        session.add(CCOperationalAgent(
            id=agent_id, tenant_id=TENANT, name=f"Agent {agent_id}",
            status="active", version=1, activated_version=1,
        ))
        if site_id:
            session.add(CCScopeGrant(
                tenant_id=TENANT, principal_type="agent",
                principal_ref=agent_id, scope_type="site", scope_ref=site_id,
                granted_by="kc-owner",
            ))
        await session.commit()


async def _site(stack, name="DC-1", node="node-1"):
    """One site with one device.

    The device id is per site: a node agent id identifies a node, so two
    sites holding the same one is an estate that cannot exist -- and the
    fleet lookup rightly raises on it.
    """
    async with stack.sessionmaker() as session:
        site = CCSite(tenant_id=TENANT, site_name=name,
                      sm_endpoint="sm:50051", sm_token="tok")
        session.add(site)
        await session.flush()
        session.add(CCFleetCache(
            site_id=site.id, agent_id=node, agent_name=node,
            vendor="Dell", model="R750", observation="observed",
        ))
        await session.commit()
        return site.id


async def _work(
    stack, site_id, *, agent="agent-A", status="awaiting_approval",
    directive_id="", outcome="", basis="human_approval", key="k-1",
):
    """One submission and the proposal it produced."""
    async with stack.sessionmaker() as session:
        proposal = CCAgentProposal(
            tenant_id=TENANT, agent_id=agent, actor=f"op-agent:{agent}@v1",
            agent_version=1, site_id=site_id, device_agent_id="node-1",
            action_type="SEL_CLEAR", params={"reason": "x"},
            rationale="because", evidence={"secret": "should never leak"},
            disposition="requires_approval", disposition_reason="needs a human",
            authorization_basis=basis, status=status, dedupe_key=key,
            directive_id=directive_id, outcome=outcome,
            dispatched_at=datetime.now(timezone.utc) if directive_id else None,
            outcome_at=datetime.now(timezone.utc) if outcome else None,
        )
        session.add(proposal)
        await session.flush()
        submission = CCAgentSubmission(
            tenant_id=TENANT, agent_id=agent, agent_version=1,
            idempotency_key=key, request_digest="d", candidate_ref="c",
            proposal_id=proposal.id, code="", reason="",
        )
        session.add(submission)
        await session.commit()
        return submission.id, proposal.id


async def _refusal(stack, *, agent="agent-A", key="k-refused"):
    async with stack.sessionmaker() as session:
        row = CCAgentSubmission(
            tenant_id=TENANT, agent_id=agent, agent_version=1,
            idempotency_key=key, request_digest="d", candidate_ref="c",
            proposal_id=None, code="candidate_not_current",
            reason="this candidate is not among the actions the agent "
                   "would propose right now",
        )
        session.add(row)
        await session.commit()
        return row.id


# ---------------------------------------------------------------------------
# 1. Ownership: an id is not a credential
# ---------------------------------------------------------------------------


class TestAnIdentifierIsNotACredential:
    async def test_an_agent_reads_its_own_submission(self):
        stack = await _stack()
        site_id = await _site(stack)
        sub_id, prop_id = await _work(stack, site_id)
        async with stack.as_machine("agent-A").client() as c:
            res = await c.get(
                f"/api/operational-agents/agent-A/submissions/{sub_id}")
        assert res.status_code == 200
        body = res.json()
        assert body["submission"]["submission_id"] == sub_id
        assert body["proposal"]["proposal_id"] == prop_id

    async def test_agent_b_cannot_read_agent_a_submission(self):
        """404, not 403: the id is the thing being guessed."""
        stack = await _stack()
        site_id = await _site(stack)
        sub_id, _ = await _work(stack, site_id, agent="agent-A")
        async with stack.as_machine("agent-B").client() as c:
            res = await c.get(
                f"/api/operational-agents/agent-B/submissions/{sub_id}")
        assert res.status_code == 404

    async def test_agent_b_cannot_read_agent_a_proposal(self):
        stack = await _stack()
        site_id = await _site(stack)
        _, prop_id = await _work(stack, site_id, agent="agent-A")
        async with stack.as_machine("agent-B").client() as c:
            res = await c.get(
                f"/api/operational-agents/agent-B/proposals/{prop_id}")
        assert res.status_code == 404

    async def test_naming_another_agent_in_the_path_is_refused(self):
        """A25.5: the token decides which agent, never the path."""
        stack = await _stack()
        site_id = await _site(stack)
        sub_id, _ = await _work(stack, site_id, agent="agent-A")
        async with stack.as_machine("agent-B").client() as c:
            res = await c.get(
                f"/api/operational-agents/agent-A/submissions/{sub_id}")
        assert res.status_code == 403
        assert "own agent and no other" in res.json()["detail"]

    async def test_a_guessed_identifier_is_not_found(self):
        stack = await _stack()
        await _site(stack)
        async with stack.as_machine("agent-A").client() as c:
            res = await c.get(
                "/api/operational-agents/agent-A/submissions/"
                + "0" * 32)
        assert res.status_code == 404

    async def test_a_human_is_refused_this_surface(self):
        stack = await _stack()
        site_id = await _site(stack)
        sub_id, _ = await _work(stack, site_id)
        stack.as_person(role="tenant_owner")
        async with stack.client() as c:
            res = await c.get(
                f"/api/operational-agents/agent-A/submissions/{sub_id}")
        assert res.status_code == 403
        assert "machine-principal surface" in res.json()["detail"]


# ---------------------------------------------------------------------------
# 2. The historical receipt (A25.2)
# ---------------------------------------------------------------------------


class TestTheHistoricalReceipt:
    async def _revoked_view(self, stack, sub_id):
        # Current authority reaches no site at all.
        stack.narrow_to(set())
        async with stack.as_machine("agent-A").client() as c:
            return await c.get(
                f"/api/operational-agents/agent-A/submissions/{sub_id}")

    async def test_an_agent_can_still_close_its_own_transaction(self):
        stack = await _stack()
        site_id = await _site(stack)
        sub_id, _ = await _work(
            stack, site_id, status="completed", directive_id="dir-1",
            outcome="SUCCESS")
        res = await self._revoked_view(stack, sub_id)
        assert res.status_code == 200
        body = res.json()
        assert body["view"] == "historical_receipt"
        assert body["authority"] == "historical_attribution_only"
        assert body["proposal"]["status"] == "completed"
        assert body["outcome"]["classification"] == "SUCCESS"
        assert body["terminal"]["terminal"] is True

    async def test_the_receipt_carries_no_estate_detail(self):
        """Asserted by NAME, because exclusion-filtering leaks the next
        field somebody adds upstream."""
        stack = await _stack()
        site_id = await _site(stack)
        sub_id, _ = await _work(
            stack, site_id, status="completed", directive_id="dir-1",
            outcome="SUCCESS")
        body = (await self._revoked_view(stack, sub_id)).json()
        proposal = body["proposal"]
        for forbidden in ("device_agent_id", "site_id", "params",
                          "action_type", "blocking_conditions",
                          "authorization_basis", "evidence", "rationale"):
            assert forbidden not in proposal, forbidden
        assert "dispatch_reason" not in body["execution"]
        # And nothing anywhere in the document mentions the estate.
        import json
        blob = json.dumps(body)
        assert "node-1" not in blob
        assert site_id not in blob
        assert "should never leak" not in blob

    async def test_even_a_full_view_withholds_execution_internals(self):
        """`full` is a question about the ESTATE, not about internals.

        Found while fixing the machine list: `proposal_block(full=True)`
        carried executable params and the authorization basis, so a
        machine principal with current authority received an executable
        payload it never authored. Conflating estate identity with
        execution internals is what produced that.
        """
        import json

        stack = await _stack()
        site_id = await _site(stack)
        async with stack.sessionmaker() as session:
            row = CCAgentProposal(
                tenant_id=TENANT, agent_id="agent-A",
                actor="op-agent:agent-A@v1", agent_version=1,
                site_id=site_id, device_agent_id="node-1",
                action_type="SEL_CLEAR",
                params={"secret_param": "EXEC-PAYLOAD"},
                evidence={"diagnosis": "RAW-EVIDENCE"},
                disposition="requires_approval",
                authorization_basis="human_approval", status="dispatched",
                dedupe_key="k-full", directive_id="dir-INTERNAL",
                dispatch_reason="DISPATCH-INTERNALS",
                dispatched_at=datetime.now(timezone.utc),
            )
            session.add(row)
            await session.flush()
            sub = CCAgentSubmission(
                tenant_id=TENANT, agent_id="agent-A", agent_version=1,
                idempotency_key="k-full", request_digest="d",
                candidate_ref="c", proposal_id=row.id, code="", reason="",
            )
            session.add(sub)
            await session.commit()
            sub_id = sub.id
        async with stack.as_machine("agent-A").client() as c:
            body = (await c.get(
                f"/api/operational-agents/agent-A/submissions/{sub_id}")).json()
        assert body["view"] == "full"
        blob = json.dumps(body)
        for forbidden in ("EXEC-PAYLOAD", "RAW-EVIDENCE", "dir-INTERNAL",
                          "DISPATCH-INTERNALS", "authorization_basis"):
            assert forbidden not in blob, forbidden
        # And it still answers the lifecycle.
        assert body["proposal"]["device_agent_id"] == "node-1"
        assert body["execution"]["directive_issued"] is True

    async def test_a_full_view_does_carry_estate_detail(self):
        """The narrowing must be the exception, not the default."""
        stack = await _stack()
        site_id = await _site(stack)
        sub_id, _ = await _work(stack, site_id)
        async with stack.as_machine("agent-A").client() as c:
            body = (await c.get(
                f"/api/operational-agents/agent-A/submissions/{sub_id}")).json()
        assert body["view"] == "full"
        assert body["proposal"]["device_agent_id"] == "node-1"
        assert body["proposal"]["site_id"] == site_id

    async def test_scope_narrowed_to_another_site_also_narrows(self):
        stack = await _stack()
        site_id = await _site(stack)
        other = await _site(stack, name="DC-2", node="node-2")
        sub_id, _ = await _work(stack, site_id)
        stack.narrow_to({other})
        async with stack.as_machine("agent-A").client() as c:
            body = (await c.get(
                f"/api/operational-agents/agent-A/submissions/{sub_id}")).json()
        assert body["view"] == "historical_receipt"

    async def test_a_refusal_receipt_needs_no_estate_at_all(self):
        """A submission that produced no proposal touches no site."""
        stack = await _stack()
        sub_id = await _refusal(stack)
        stack.narrow_to(set())
        async with stack.as_machine("agent-A").client() as c:
            res = await c.get(
                f"/api/operational-agents/agent-A/submissions/{sub_id}")
        assert res.status_code == 200
        body = res.json()
        assert body["submission"]["accepted"] is False
        assert body["submission"]["code"] == "candidate_not_current"
        assert body["proposal"] == {}

    async def test_the_exception_does_not_open_the_estate(self):
        """A25.2 is not an A23 bypass: ordinary reads stay narrowed."""
        stack = await _stack()
        site_id = await _site(stack)
        await _work(stack, site_id)
        stack.narrow_to(set())
        async with stack.as_machine("agent-A").client() as c:
            res = await c.get("/api/fleet/")
        # Whatever the fleet read answers, it must not have been widened
        # by the receipt rule.
        assert res.status_code in (200, 403, 404)
        if res.status_code == 200:
            assert res.json().get("devices", []) == []


# ---------------------------------------------------------------------------
# 3. Six layers, not one status (A25.4)
# ---------------------------------------------------------------------------


class TestTheLayersStaySeparate:
    async def _receipt(self, stack, sub_id, agent="agent-A"):
        async with stack.as_machine(agent).client() as c:
            return (await c.get(
                f"/api/operational-agents/{agent}/submissions/{sub_id}")).json()

    async def test_the_six_blocks_are_present(self):
        stack = await _stack()
        site_id = await _site(stack)
        sub_id, _ = await _work(stack, site_id)
        body = await self._receipt(stack, sub_id)
        for block in ("submission", "proposal", "approval", "execution",
                      "outcome", "terminal"):
            assert block in body, block
        assert "status" not in body, (
            "a flattened top-level status would collapse the layers A25.4 "
            "keeps apart"
        )

    async def test_approved_is_not_terminal(self):
        """The budget can return it to the queue; a cache must not hide that."""
        stack = await _stack()
        site_id = await _site(stack)
        sub_id, _ = await _work(stack, site_id, status="approved")
        body = await self._receipt(stack, sub_id)
        assert body["proposal"]["status"] == "approved"
        assert body["terminal"]["terminal"] is False
        assert "never executed" in body["terminal"]["note"]

    @pytest.mark.parametrize("status,terminal,layer", [
        ("proposed", False, ""),
        ("awaiting_approval", False, ""),
        ("approved", False, ""),
        ("dispatched", False, ""),
        ("denied", True, "approval"),
        ("blocked", True, "governance"),
        ("completed", True, "outcome"),
        ("failed", True, "outcome"),
    ])
    async def test_terminality_follows_the_lifecycle(self, status, terminal, layer):
        stack = await _stack()
        site_id = await _site(stack)
        sub_id, _ = await _work(stack, site_id, status=status, key=f"k-{status}")
        body = await self._receipt(stack, sub_id)
        assert body["terminal"]["terminal"] is terminal
        assert body["terminal"]["terminal_layer"] == layer

    @pytest.mark.parametrize("classification", ["PARTIAL", "ROLLBACK", "FAILURE"])
    async def test_a_partial_is_not_reported_as_a_plain_failure(self, classification):
        stack = await _stack()
        site_id = await _site(stack)
        sub_id, _ = await _work(
            stack, site_id, status="failed", directive_id="dir-1",
            outcome=classification, key=f"k-{classification}")
        body = await self._receipt(stack, sub_id)
        assert body["proposal"]["status"] == "failed"
        assert body["outcome"]["classification"] == classification, (
            "the canonical classification must travel beside the collapse"
        )

    async def test_execution_does_not_leak_the_internal_handle(self):
        """D3: internal correlation handles stay internal."""
        stack = await _stack()
        site_id = await _site(stack)
        sub_id, _ = await _work(stack, site_id, status="dispatched",
                                directive_id="dir-SECRET")
        body = await self._receipt(stack, sub_id)
        assert body["execution"]["directive_issued"] is True
        import json
        assert "dir-SECRET" not in json.dumps(body)


# ---------------------------------------------------------------------------
# 4. Approver identity (A25.3)
# ---------------------------------------------------------------------------


class TestApproverIdentityIsNeverMachineVisible:
    async def test_the_receipt_reports_that_not_who(self):
        stack = await _stack()
        site_id = await _site(stack)
        sub_id, prop_id = await _work(stack, site_id, status="approved")
        async with stack.sessionmaker() as session:
            row = await session.get(CCAgentProposal, prop_id)
            row.decided_by = "alice@example.com"
            row.decided_at = datetime.now(timezone.utc)
            await session.commit()
        async with stack.as_machine("agent-A").client() as c:
            body = (await c.get(
                f"/api/operational-agents/agent-A/submissions/{sub_id}")).json()
        import json
        blob = json.dumps(body)
        assert "alice@example.com" not in blob
        assert "decided_by" not in blob
        approval = body["approval"]
        assert set(approval) == {
            "required", "state", "granted_count", "required_count", "decided_at",
        }

    def test_the_block_cannot_pass_through_a_field_it_did_not_name(self):
        """Fed a completion carrying identities, it must emit none of them.

        The first version of this test grepped the function's source --
        and failed, because the docstring NAMES the excluded fields in
        order to explain them. Testing prose proves nothing; feeding the
        function hostile input proves the property. It also catches the
        real regression: somebody replacing the explicit keys with
        `block.update(completion)`.
        """
        from harkeniq_cc.receipts import approval_block

        class Proposal:
            authorization_basis = "human_approval"
            decided_at = None

        poisoned = {
            "state": "approved",
            "required": 2,
            "received": 2,
            "approvers": [{"approver": "alice@example.com"}],
            "denied_by": "bob@example.com",
            "denied_reason": "no",
            "policy_name": "Dual authorization",
            "group_name": "SRE leads",
        }
        block = approval_block(poisoned, Proposal())
        assert set(block) == {
            "required", "state", "granted_count", "required_count", "decided_at",
        }
        import json
        blob = json.dumps(block)
        for leaked in ("alice", "bob", "Dual authorization", "SRE leads"):
            assert leaked not in blob, leaked


# ---------------------------------------------------------------------------
# 5. Purity, caching and metering (A25.6, A25.7)
# ---------------------------------------------------------------------------


class TestReadsAreSafe:
    async def test_a_read_writes_no_governance_state(self):
        import sqlalchemy as sa

        stack = await _stack()
        site_id = await _site(stack)
        sub_id, _ = await _work(stack, site_id)
        async with stack.sessionmaker() as session:
            before = [
                (p.id, p.status, p.outcome)
                for p in (await session.execute(
                    sa.select(CCAgentProposal))).scalars().all()
            ]
        async with stack.as_machine("agent-A").client() as c:
            for _ in range(3):
                await c.get(
                    f"/api/operational-agents/agent-A/submissions/{sub_id}")
        async with stack.sessionmaker() as session:
            after = [
                (p.id, p.status, p.outcome)
                for p in (await session.execute(
                    sa.select(CCAgentProposal))).scalars().all()
            ]
        assert before == after

    async def test_a_terminal_receipt_is_cacheable_and_revalidates(self):
        stack = await _stack()
        site_id = await _site(stack)
        sub_id, _ = await _work(stack, site_id, status="completed",
                                directive_id="dir-1", outcome="SUCCESS")
        async with stack.as_machine("agent-A").client() as c:
            first = await c.get(
                f"/api/operational-agents/agent-A/submissions/{sub_id}")
            etag = first.headers.get("etag")
            assert etag, "a terminal receipt should carry an ETag"
            again = await c.get(
                f"/api/operational-agents/agent-A/submissions/{sub_id}",
                headers={"If-None-Match": etag})
        assert again.status_code == 304

    async def test_a_non_terminal_receipt_is_never_cacheable(self):
        """A25.7: caching may not conceal approved -> awaiting_approval."""
        stack = await _stack()
        site_id = await _site(stack)
        sub_id, _ = await _work(stack, site_id, status="approved")
        async with stack.as_machine("agent-A").client() as c:
            res = await c.get(
                f"/api/operational-agents/agent-A/submissions/{sub_id}")
        assert "etag" not in {k.lower() for k in res.headers}
        assert res.headers.get("cache-control") == "no-store"

    async def test_polling_is_bounded(self, monkeypatch):
        """Pinned to a FIXED window, not to the wall clock.

        The first version issued `READ_MAX_PER_WINDOW + 2` real requests
        and asserted a 429 appeared. It passed alone and failed inside the
        module, because a loop that long can straddle a window boundary,
        reset the count, and never reach the limit -- a flake of my own
        making. The limit is a property of the counter, so the window is
        held still and the property is asserted directly.
        """
        from harkeniq_cc import ingress_limits
        from harkeniq_cc.db.repos import AgentReadWindowRepo
        from harkeniq_cc.ingress_limits import READ_MAX_PER_WINDOW

        fixed = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
        monkeypatch.setattr(
            ingress_limits, "read_window_start", lambda now=None: fixed
        )

        stack = await _stack()
        site_id = await _site(stack)
        sub_id, _ = await _work(stack, site_id)

        # Spend the whole window, then ask for one more.
        async with stack.sessionmaker() as session:
            repo = AgentReadWindowRepo(session)
            for _ in range(READ_MAX_PER_WINDOW):
                await repo.increment(
                    tenant_id=TENANT, agent_id="agent-A", window_start=fixed)
            await session.commit()

        url = f"/api/operational-agents/agent-A/submissions/{sub_id}"
        async with stack.as_machine("agent-A").client() as c:
            assert (await c.get(url)).status_code == 429, (
                "status polling was unbounded"
            )

    async def test_the_proposal_list_shares_the_same_polling_budget(self):
        """MEDIUM 2: alternating endpoints must not multiply the allowance."""
        import sqlalchemy as sa

        from harkeniq_cc.db.models import CCAgentReadWindow

        stack = await _stack()
        site_id = await _site(stack)
        sub_id, prop_id = await _work(stack, site_id)
        await _agent_row(stack, site_id=site_id)
        async with stack.as_machine("agent-A").client() as c:
            await c.get(
                f"/api/operational-agents/agent-A/submissions/{sub_id}")
            await c.get(
                f"/api/operational-agents/agent-A/proposals/{prop_id}")
            await c.get("/api/operational-agents/agent-A/proposals")
        async with stack.sessionmaker() as session:
            total = sum(w.reads for w in (await session.execute(
                sa.select(CCAgentReadWindow))).scalars().all())
        assert total == 3, (
            f"three machine status reads counted as {total}: the proposal "
            "list is an unmetered substitute for the receipt endpoints"
        )

    async def test_a_human_list_read_is_not_charged_to_an_agent(self):
        """A human administrator is governed by RBAC, not by a polling budget."""
        import sqlalchemy as sa

        from harkeniq_cc.db.models import CCAgentReadWindow

        stack = await _stack()
        site_id = await _site(stack)
        await _work(stack, site_id)
        await _agent_row(stack, site_id=site_id)
        stack.as_person(role="tenant_owner")
        async with stack.client() as c:
            res = await c.get("/api/operational-agents/agent-A/proposals")
        assert res.status_code == 200
        async with stack.sessionmaker() as session:
            rows = (await session.execute(
                sa.select(CCAgentReadWindow))).scalars().all()
        assert rows == [], "a human read was charged to a machine bucket"

    async def test_read_accounting_is_separate_from_submission_attempts(self):
        """A25.6: a poll must not be counted as a governed attempt."""
        import sqlalchemy as sa

        from harkeniq_cc.db.models import CCAgentIngressAttempt, CCAgentReadWindow

        stack = await _stack()
        site_id = await _site(stack)
        sub_id, _ = await _work(stack, site_id)
        async with stack.as_machine("agent-A").client() as c:
            for _ in range(3):
                await c.get(
                    f"/api/operational-agents/agent-A/submissions/{sub_id}")
        async with stack.sessionmaker() as session:
            attempts = (await session.execute(
                sa.select(CCAgentIngressAttempt))).scalars().all()
            windows = (await session.execute(
                sa.select(CCAgentReadWindow))).scalars().all()
        assert attempts == [], (
            "status reads were counted in the governed submission ledger"
        )
        assert windows and sum(w.reads for w in windows) == 3

    async def test_polling_does_not_enter_the_audit_chain(self):
        """A25.6: a polling loop is not governance history."""
        import sqlalchemy as sa

        from harkeniq_cc.db.models import CCAuditLog

        stack = await _stack()
        site_id = await _site(stack)
        sub_id, _ = await _work(stack, site_id)
        async with stack.sessionmaker() as session:
            before = len((await session.execute(
                sa.select(CCAuditLog))).scalars().all())
        async with stack.as_machine("agent-A").client() as c:
            for _ in range(5):
                await c.get(
                    f"/api/operational-agents/agent-A/submissions/{sub_id}")
        async with stack.sessionmaker() as session:
            after = len((await session.execute(
                sa.select(CCAuditLog))).scalars().all())
        assert after == before


# ---------------------------------------------------------------------------
# 6. Tenancy and the A6-1 replay gap
# ---------------------------------------------------------------------------


class TestTenancyAndReplay:
    async def test_a_submission_from_another_tenant_is_not_found(self):
        """The repository read is tenant-filtered; this proves it."""
        stack = await _stack()
        site_id = await _site(stack)
        async with stack.sessionmaker() as session:
            foreign = CCAgentSubmission(
                tenant_id="other-tenant", agent_id="agent-A", agent_version=1,
                idempotency_key="k-foreign", request_digest="d",
                candidate_ref="c", proposal_id=None, code="", reason="",
            )
            session.add(foreign)
            await session.commit()
            foreign_id = foreign.id
        assert site_id
        async with stack.as_machine("agent-A").client() as c:
            res = await c.get(
                f"/api/operational-agents/agent-A/submissions/{foreign_id}")
        assert res.status_code == 404

    async def test_a_proposal_from_another_tenant_is_not_found(self):
        stack = await _stack()
        async with stack.sessionmaker() as session:
            foreign = CCAgentProposal(
                tenant_id="other-tenant", agent_id="agent-A",
                actor="op-agent:agent-A@v1", agent_version=1, site_id="s-x",
                device_agent_id="node-x", action_type="SEL_CLEAR",
                status="completed", dedupe_key="k-foreign",
            )
            session.add(foreign)
            await session.commit()
            foreign_id = foreign.id
        async with stack.as_machine("agent-A").client() as c:
            res = await c.get(
                f"/api/operational-agents/agent-A/proposals/{foreign_id}")
        assert res.status_code == 404

    async def test_a_replay_can_be_resolved_to_current_state(self):
        """The A6-1 gap this slice closes.

        A replayed submit returns 200 with `proposal_id` and no state, so
        the call a retrying runtime makes most often told it least. The
        receipt is what turns that identifier back into an answer.
        """
        stack = await _stack()
        site_id = await _site(stack)
        sub_id, prop_id = await _work(
            stack, site_id, status="dispatched", directive_id="dir-1")
        async with stack.as_machine("agent-A").client() as c:
            body = (await c.get(
                f"/api/operational-agents/agent-A/submissions/{sub_id}")).json()
        assert body["submission"]["submission_id"] == sub_id
        assert body["proposal"]["proposal_id"] == prop_id
        assert body["proposal"]["status"] == "dispatched"
        assert body["execution"]["dispatched"] is True
        assert body["terminal"]["terminal"] is False

    async def test_the_outcome_classification_comes_from_the_exact_join(self):
        """A25.1 reaching the external contract: the receipt reports the
        canonical classification, not the two-value collapse."""
        stack = await _stack()
        site_id = await _site(stack)
        sub_id, _ = await _work(
            stack, site_id, status="failed", directive_id="dir-J",
            outcome="PARTIAL")
        async with stack.sessionmaker() as session:
            session.add(CCOutcomeHistory(
                site_id=site_id, action_id="directive:dir-J",
                action_type="SEL_CLEAR", device_agent_id="node-1",
                outcome="PARTIAL", fault_resolved=False,
                actor="op-agent:agent-A@v1",
                ingested_at=datetime.now(timezone.utc),
            ))
            await session.commit()
        async with stack.as_machine("agent-A").client() as c:
            body = (await c.get(
                f"/api/operational-agents/agent-A/submissions/{sub_id}")).json()
        assert body["outcome"]["classification"] == "PARTIAL"
        assert body["outcome"]["fault_resolved"] is False


# ---------------------------------------------------------------------------
# 7. Approval completion comes from the CANONICAL policy (A25.3 / one system)
# ---------------------------------------------------------------------------


class TestApprovalCompletionIsCanonical:
    """The receipt must ask the approval system, not count records.

    The first implementation called `evaluate_completion(records,
    len(records))`, deriving "how many are needed" from "how many have
    decided". A tenant with dual authorization configured and one
    approval recorded would have been told, over a machine contract, that
    the subject was APPROVED -- E0.1's own defect arriving at a fifth
    origin.
    """

    async def _with_policy(self, stack, *, required, approvals, denied=False):
        from harkeniq_cc.approval_policy import SUBJECT_AGENT_PROPOSAL
        from harkeniq_cc.db.models import CCApprovalPolicy, CCApprovalRecord

        site_id = await _site(stack)
        sub_id, prop_id = await _work(stack, site_id, status="awaiting_approval")
        async with stack.sessionmaker() as session:
            session.add(CCApprovalPolicy(
                tenant_id=TENANT, name="dual", action_type="*",
                device_type="*", risk_level="*",
                required_approvers=required, approval_mode="require_approval",
                created_by="kc-owner",
            ))
            for n in range(approvals):
                session.add(CCApprovalRecord(
                    tenant_id=TENANT, subject_type=SUBJECT_AGENT_PROPOSAL,
                    subject_ref=prop_id, approver_ref=f"kc-approver-{n}",
                    approver_email=f"approver{n}@example.com",
                    decision="approved", scope_ok=True,
                    decided_at=datetime.now(timezone.utc),
                ))
            if denied:
                session.add(CCApprovalRecord(
                    tenant_id=TENANT, subject_type=SUBJECT_AGENT_PROPOSAL,
                    subject_ref=prop_id, approver_ref="kc-objector",
                    approver_email="objector@example.com",
                    decision="denied", scope_ok=True, reason="no",
                    decided_at=datetime.now(timezone.utc),
                ))
            await session.commit()
        return sub_id

    async def _approval(self, stack, sub_id):
        async with stack.as_machine("agent-A").client() as c:
            body = (await c.get(
                f"/api/operational-agents/agent-A/submissions/{sub_id}")).json()
        return body["approval"]

    async def test_two_required_none_received_is_pending(self):
        stack = await _stack()
        sub_id = await self._with_policy(stack, required=2, approvals=0)
        block = await self._approval(stack, sub_id)
        assert block["state"] == "pending"
        assert (block["granted_count"], block["required_count"]) == (0, 2)

    async def test_two_required_one_received_is_still_pending(self):
        """THE regression. `len(records)` would have said approved."""
        stack = await _stack()
        sub_id = await self._with_policy(stack, required=2, approvals=1)
        block = await self._approval(stack, sub_id)
        assert block["state"] == "pending", (
            "one approval completed a two-approver policy -- the receipt is "
            "deriving the requirement from the records instead of the policy"
        )
        assert (block["granted_count"], block["required_count"]) == (1, 2)

    async def test_two_required_two_received_is_approved(self):
        stack = await _stack()
        sub_id = await self._with_policy(stack, required=2, approvals=2)
        block = await self._approval(stack, sub_id)
        assert block["state"] == "approved"
        assert (block["granted_count"], block["required_count"]) == (2, 2)

    async def test_a_denial_is_terminal_whatever_the_count(self):
        """D16: one objection outranks any number of approvals."""
        stack = await _stack()
        sub_id = await self._with_policy(stack, required=2, approvals=2, denied=True)
        block = await self._approval(stack, sub_id)
        assert block["state"] == "denied"

    async def test_the_canonical_path_is_the_one_being_called(self):
        """Structural: one approval system, asked through its own door."""
        import inspect

        from harkeniq_cc import receipts

        # Strip the docstring before reading the code. This module has
        # now twice written a structural test that failed on its own
        # prose -- the docstring here deliberately QUOTES the defective
        # call in order to explain it.
        import ast
        import textwrap

        tree = ast.parse(textwrap.dedent(
            inspect.getsource(receipts.approval_completion)
        ))
        fn = tree.body[0]
        if (
            fn.body and isinstance(fn.body[0], ast.Expr)
            and isinstance(fn.body[0].value, ast.Constant)
        ):
            fn.body = fn.body[1:]
        code = ast.unparse(fn)

        assert "governing_policy" in code, (
            "the receipt resolves no policy -- it is guessing the requirement"
        )
        assert "canonical_block" in code
        assert "len(records)" not in code, (
            "the requirement is being derived from the records again"
        )

    async def test_identities_still_never_escape_the_canonical_block(self):
        """The canonical block carries emails; the projection must not."""
        import json

        stack = await _stack()
        sub_id = await self._with_policy(stack, required=2, approvals=2)
        block = await self._approval(stack, sub_id)
        assert set(block) == {
            "required", "state", "granted_count", "required_count", "decided_at",
        }
        assert "approver0@example.com" not in json.dumps(block)


# ---------------------------------------------------------------------------
# 8. The machine proposal LIST (HIGH 2)
# ---------------------------------------------------------------------------


class TestTheMachineProposalList:
    """`list_proposals` became machine-self-readable in this slice.

    The authorization moved and the PROJECTION did not, so an external
    runtime would have received the Console's payload: approver identity,
    raw evidence, executable params, directive id, dispatch internals.
    """

    async def _poisoned(self, stack):
        """A proposal carrying every field that must not reach a machine."""
        site_id = await _site(stack)
        await _agent_row(stack, site_id=site_id)
        async with stack.sessionmaker() as session:
            row = CCAgentProposal(
                tenant_id=TENANT, agent_id="agent-A",
                actor="op-agent:agent-A@v1", agent_version=1,
                site_id=site_id, device_agent_id="node-1",
                action_type="SEL_CLEAR",
                params={"secret_param": "EXEC-PAYLOAD"},
                rationale="because",
                evidence={"diagnosis": "RAW-EVIDENCE-BLOB"},
                disposition="requires_approval",
                disposition_reason="needs a human",
                blocking_conditions=[{"code": "x", "detail": "BLOCKING-DETAIL"}],
                authorization_basis="human_approval",
                status="dispatched",
                decided_by="alice@example.com",
                decided_at=datetime.now(timezone.utc),
                dedupe_key="k-poison", directive_id="dir-INTERNAL",
                dispatch_reason="DISPATCH-INTERNALS",
                dispatched_at=datetime.now(timezone.utc),
            )
            session.add(row)
            await session.commit()
        return site_id

    async def test_no_forbidden_field_appears_anywhere_in_the_response(self):
        """Hostile serialization: search the whole document, not the keys."""
        import json

        stack = await _stack()
        await self._poisoned(stack)
        async with stack.as_machine("agent-A").client() as c:
            res = await c.get("/api/operational-agents/agent-A/proposals")
        assert res.status_code == 200, res.text
        blob = json.dumps(res.json())
        for forbidden in (
            "alice@example.com",     # approver identity
            "decided_by",            # the field itself
            "RAW-EVIDENCE-BLOB",     # raw evidence
            "EXEC-PAYLOAD",          # executable params
            "dir-INTERNAL",          # internal correlation handle
            "DISPATCH-INTERNALS",    # dispatch internals
        ):
            assert forbidden not in blob, forbidden
        assert res.json()["view"] == "machine"

    async def test_the_machine_list_still_answers_the_lifecycle(self):
        """Withholding must not make the list useless."""
        stack = await _stack()
        await self._poisoned(stack)
        async with stack.as_machine("agent-A").client() as c:
            body = (await c.get(
                "/api/operational-agents/agent-A/proposals")).json()
        item = body["proposals"][0]
        assert item["proposal"]["status"] == "dispatched"
        assert item["execution"]["dispatched"] is True
        assert item["execution"]["directive_issued"] is True
        assert item["terminal"]["terminal"] is False
        assert set(item["approval"]) == {
            "required", "state", "granted_count", "required_count", "decided_at",
        }

    async def test_a_human_administrator_still_gets_the_rich_projection(self):
        """The Console's payload is correct for a human and must survive."""
        import json

        stack = await _stack()
        await self._poisoned(stack)
        stack.as_person(role="tenant_owner")
        async with stack.client() as c:
            res = await c.get("/api/operational-agents/agent-A/proposals")
        assert res.status_code == 200
        blob = json.dumps(res.json())
        assert "alice@example.com" in blob, (
            "the human administrator lost the authorized rich projection"
        )
        assert "RAW-EVIDENCE-BLOB" in blob
        assert res.json().get("view") != "machine"

    async def test_the_list_and_the_receipt_cannot_disagree(self):
        """One set of blocks, so a list item and a receipt match."""
        stack = await _stack()
        site_id = await _site(stack)
        await _agent_row(stack, site_id=site_id)
        sub_id, prop_id = await _work(stack, site_id, status="dispatched",
                                      directive_id="dir-1")
        async with stack.as_machine("agent-A").client() as c:
            listed = (await c.get(
                "/api/operational-agents/agent-A/proposals")).json()
            receipt = (await c.get(
                f"/api/operational-agents/agent-A/submissions/{sub_id}")).json()
        item = next(
            i for i in listed["proposals"] if i["proposal_id"] == prop_id
        )
        for block in ("proposal", "approval", "execution", "terminal"):
            assert item[block] == receipt[block], block


class TestTheReadCounterKeepsItsTransactionContract:
    """LOW: a helper may not roll back work it never knew about."""

    async def test_losing_the_open_race_does_not_discard_caller_work(self):
        import sqlalchemy as sa

        from harkeniq_cc.db.models import CCAgentReadWindow, CCSite
        from harkeniq_cc.db.repos import AgentReadWindowRepo
        from harkeniq_cc.ingress_limits import read_window_start

        stack = await _stack()
        window = read_window_start()
        # Another replica has already opened this window.
        async with stack.sessionmaker() as session:
            session.add(CCAgentReadWindow(
                tenant_id=TENANT, agent_id="agent-A",
                window_start=window, reads=1))
            await session.commit()

        async with stack.sessionmaker() as session:
            # Caller work that must survive the increment's lost race.
            session.add(CCSite(
                tenant_id=TENANT, site_name="caller-work",
                sm_endpoint="sm:1", sm_token="t"))
            await session.flush()
            used = await AgentReadWindowRepo(session).increment(
                tenant_id=TENANT, agent_id="agent-A", window_start=window)
            await session.commit()

        assert used == 2
        async with stack.sessionmaker() as session:
            sites = (await session.execute(
                sa.select(CCSite).where(CCSite.site_name == "caller-work")
            )).scalars().all()
        assert len(sites) == 1, (
            "the read counter rolled back the caller's transaction"
        )

    def test_it_does_not_call_session_rollback(self):
        """Structural: the shape that caused it must not return."""
        import ast
        import inspect
        import textwrap

        from harkeniq_cc.db.repos import AgentReadWindowRepo

        tree = ast.parse(textwrap.dedent(
            inspect.getsource(AgentReadWindowRepo.increment)))
        fn = tree.body[0]
        if (
            fn.body and isinstance(fn.body[0], ast.Expr)
            and isinstance(fn.body[0].value, ast.Constant)
        ):
            fn.body = fn.body[1:]
        code = ast.unparse(fn)
        assert "session.rollback()" not in code
        assert "begin_nested" in code
