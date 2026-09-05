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

    def as_machine(self, agent_id, permissions=("fleet.view", "incident.view")):
        self.machine = (agent_id, list(permissions))
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


async def _site(stack, name="DC-1"):
    async with stack.sessionmaker() as session:
        site = CCSite(tenant_id=TENANT, site_name=name,
                      sm_endpoint="sm:50051", sm_token="tok")
        session.add(site)
        await session.flush()
        session.add(CCFleetCache(
            site_id=site.id, agent_id="node-1", agent_name="n1",
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
        other = await _site(stack, name="DC-2")
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

    async def test_polling_is_bounded(self):
        from harkeniq_cc.ingress_limits import READ_MAX_PER_WINDOW

        stack = await _stack()
        site_id = await _site(stack)
        sub_id, _ = await _work(stack, site_id)
        url = f"/api/operational-agents/agent-A/submissions/{sub_id}"
        async with stack.as_machine("agent-A").client() as c:
            codes = set()
            for _ in range(READ_MAX_PER_WINDOW + 2):
                codes.add((await c.get(url)).status_code)
        assert 429 in codes, "status polling was unbounded"

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
