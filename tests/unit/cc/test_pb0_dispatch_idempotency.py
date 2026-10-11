"""PB-0 (A30.42/A30.43): one proposal -> at most one directive.

Before PB-0 an approved Operational Agent proposal could be delivered to
the Site Manager more than once -- a lost acknowledgement, a second
Central Command replica, a mid-pass commit failure, two approvers finishing
together -- and the Site Manager queued every delivery. Two directives, two
executions, two charges against the site's error budget.

These tests drive Central Command's REAL dispatch paths against a REAL
Site Manager gRPC server, through Central Command's own `SMClient`, and
count directives in the Site Manager's own database. The fault each test
injects is the only thing that is not production code: an acknowledgement
that never arrives, a crash between claim and send, a second dispatcher.

True concurrency (two replicas, two approvers racing on PostgreSQL row
locks) is proven on real PostgreSQL in
`tests/integration/test_pb0_dispatch_idempotency_pg.py`; sqlite here shares
one connection between sessions, so these prove the sequential semantics.
"""

from __future__ import annotations

import ast
import json
import pathlib
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import func, select, update

from harkeniq.dispatch_idempotency import (
    KEY_PREFIX,
    REPLAY_REASON,
    idempotency_key,
    is_replay,
)
from harkeniq.proto import harkeniq_pb2, harkeniq_pb2_grpc
from harkeniq_cc import agent_runtime
from harkeniq_cc.api import approvals as approvals_api
from harkeniq_cc.api.deps import get_current_user
from harkeniq_cc.app import create_app
from harkeniq_cc.auth import ROLE_PERMISSIONS, UserContext, configure_auth
from harkeniq_cc.config import CCConfig
from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_cc.db.models import (
    CCAgentProposal,
    CCAuditLog,
    CCAutonomyBudget,
    CCFleetCache,
    CCIncident,
    CCSafetyState,
    CCSite,
)
from harkeniq_cc.db.repos import AgentProposalRepo
from harkeniq_cc.runtime import AppState
from harkeniq_cc.sm_client import SMClient
from harkeniq_sm.approvals import ApprovalService
from harkeniq_sm.autonomy import SMAutonomyEnforcer
from harkeniq_sm.config import SMConfig
from harkeniq_sm.db import base as sm_base
from harkeniq_sm.db.models import AuditLogRow, DirectedDirective
from harkeniq_sm.db.repos import DeviceRepo, DirectiveRepo, SiteRepo
from harkeniq_sm.directives import DirectiveService
from harkeniq_sm.grpc_server import (
    AgentServiceServicer,
    SiteManagerServiceServicer,
    build_server,
)
from harkeniq_sm.ingest import IngestService

from tests.unit.cc.conftest import seed_tenant_people

TENANT = "t1"
TOKEN = "site-secret"
REPO = pathlib.Path(__file__).resolve().parents[3]
CC_SRC = REPO / "services/central_command/src/harkeniq_cc"


# ---------------------------------------------------------------------------
# A real Site Manager, and a Central Command pointed at it
# ---------------------------------------------------------------------------


class Estate:
    """One CC stack + one served SM, sharing nothing but the wire."""

    def __init__(self):
        self.cc_engine = None
        self.sm_engine = None
        self.server = None

    async def start(self, *, autonomous: bool = False):
        # -- Site Manager --------------------------------------------------
        self.sm_engine = sm_base.make_engine("sqlite+aiosqlite:///:memory:")
        await sm_base.create_all(self.sm_engine)
        self.sm = sm_base.make_sessionmaker(self.sm_engine)
        sm_config = SMConfig(insecure=True, site_name="site-1", grpc_port=0,
                             site_token=TOKEN)
        self.directives = DirectiveService(self.sm, sm_config)
        self.autonomy = SMAutonomyEnforcer()
        self.sm_servicer = SiteManagerServiceServicer(
            self.sm, ApprovalService(self.sm, sm_config), sm_config,
            directives=self.directives, autonomy=self.autonomy,
        )
        self.server, port = build_server(
            sm_config, AgentServiceServicer(IngestService(self.sm, sm_config)),
            sm_servicer=self.sm_servicer,
        )
        await self.server.start()
        self.sm_endpoint = f"localhost:{port}"
        async with self.sm() as session:
            site = await SiteRepo(session).get_or_create("site-1")
            site.cc_site_id = "cc-site-1"
            await DeviceRepo(session).upsert_registration(
                site_id=site.id, agent_id="node-1", vendor="dell",
            )
            await session.commit()
            self.sm_site_id = site.id

        # -- Central Command -----------------------------------------------
        config = CCConfig(tenant_id=TENANT, insecure=True)
        configure_auth("", "", "", insecure=True)
        self.cc_engine = make_engine("sqlite+aiosqlite:///:memory:")
        await create_all(self.cc_engine)
        self.cc = make_sessionmaker(self.cc_engine)
        self.state = AppState(config=config, engine=self.cc_engine,
                              sessionmaker=self.cc)
        self.app = create_app(self.state)
        await seed_tenant_people(self.cc, TENANT, [
            ("kc-owner", "tenant_owner"), ("kc-approver-2", "tenant_owner"),
        ])
        self.act_as("owner")
        async with self.cc() as session:
            site = CCSite(
                tenant_id=TENANT, site_name="DC-1",
                sm_endpoint=self.sm_endpoint, sm_token=TOKEN,
            )
            session.add(site)
            await session.flush()
            now = datetime.now(timezone.utc)
            session.add(CCSafetyState(
                site_id=site.id, tenant_id=TENANT, reported=True, as_of=now,
                ingested_at=now, sm_stop_switch=False, suppressions=[],
                error_budgets=[], site_budgets={},
            ))
            session.add(CCFleetCache(
                site_id=site.id, agent_id="node-1", agent_name="rack1-node1",
                vendor="Dell", model="R750", device_class="server",
                observation="observed", health="Critical",
            ))
            session.add(CCAutonomyBudget(
                tenant_id=TENANT, device_type="*", level=2,
                budget_limit=10, budget_period="daily",
            ))
            session.add(CCIncident(
                incident_id="inc-1", tenant_id=TENANT, site_id=site.id,
                kind="device", status="open", title="BMC event log saturated",
                device_agent_id="node-1", subsystem="log", confidence=0.9,
            ))
            await session.commit()
            self.site_id = site.id
        extra = (
            {"require_approval_always": False, "autonomy_ceiling": 2}
            if autonomous else {}
        )
        async with self.client() as c:
            self.agent_id = await _make_agent(c, self.site_id, **extra)
        return self

    def act_as(self, who: str):
        async def _user():
            return UserContext(
                user_id=f"kc-{who}", email=f"{who}@example.com",
                tenant_id=TENANT, role="tenant_owner",
                permissions=list(ROLE_PERMISSIONS["tenant_owner"]),
            )
        self.app.dependency_overrides[get_current_user] = _user

    def client(self):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test",
        )

    async def stop(self):
        if self.server is not None:
            await self.server.stop(None)
        if self.cc_engine is not None:
            await self.cc_engine.dispose()
        if self.sm_engine is not None:
            await self.sm_engine.dispose()

    # -- reads --------------------------------------------------------------

    async def directives_for(self, proposal_id: str) -> list:
        async with self.sm() as session:
            return list((await session.execute(
                select(DirectedDirective).where(
                    DirectedDirective.proposal_id == proposal_id)
                .order_by(DirectedDirective.created_at)
            )).scalars().all())

    async def all_directives(self) -> int:
        async with self.sm() as session:
            return int((await session.execute(
                select(func.count(DirectedDirective.id)))).scalar())

    async def proposal(self, proposal_id: str) -> CCAgentProposal:
        async with self.cc() as session:
            return await AgentProposalRepo(session).get(TENANT, proposal_id)

    async def only_proposal(self) -> CCAgentProposal:
        async with self.cc() as session:
            rows = list((await session.execute(
                select(CCAgentProposal))).scalars().all())
        assert len(rows) == 1, rows
        return rows[0]

    async def cc_audit(self, action: str) -> list:
        async with self.cc() as session:
            return list((await session.execute(
                select(CCAuditLog).where(CCAuditLog.action == action)
                .order_by(CCAuditLog.ts)
            )).scalars().all())

    async def sm_audit(self, action: str) -> list:
        async with self.sm() as session:
            return list((await session.execute(
                select(AuditLogRow).where(AuditLogRow.action == action)
            )).scalars().all())

    async def age_claim(self, proposal_id: str, seconds: int = 61):
        """Move a claim into the past -- the clock a crashed claimer left."""
        async with self.cc() as session:
            await session.execute(
                update(CCAgentProposal)
                .where(CCAgentProposal.id == proposal_id)
                .values(dispatch_claimed_at=(
                    datetime.now(timezone.utc) - timedelta(seconds=seconds)))
            )
            await session.commit()


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
    pre = await client.post(f"/api/operational-agents/{agent_id}/preflight")
    assert pre.status_code == 200, pre.text
    result = pre.json()
    if result.get("requires_acknowledgement"):
        ack = await client.post(f"/api/operational-agents/{agent_id}/acknowledge")
        assert ack.status_code == 200, ack.text
    if result.get("requires_activation_approval"):
        detail = await client.get(f"/api/operational-agents/{agent_id}")
        subject = detail.json()["agent"].get("activation_subject_ref")
        approved = await client.post(f"/api/approvals/{subject}/approve")
        assert approved.status_code == 200, approved.text
    activated = await client.post(f"/api/operational-agents/{agent_id}/activate")
    assert activated.status_code == 200, activated.text
    return agent_id


@pytest.fixture
async def estate():
    e = Estate()
    try:
        yield await e.start()
    finally:
        await e.stop()


@pytest.fixture
async def autonomous_estate():
    e = Estate()
    try:
        yield await e.start(autonomous=True)
    finally:
        await e.stop()


class LostAck(SMClient):
    """The Site Manager receives and ACCEPTS the call; the answer never
    arrives. Exactly what a dropped connection after the server committed
    looks like from Central Command."""

    lose = 1          # acknowledgements left to lose
    sent: list = []

    async def dispatch_action(self, *a, **kw):
        result = await super().dispatch_action(*a, **kw)
        LostAck.sent.append(result)
        if LostAck.lose > 0:
            LostAck.lose -= 1
            raise ConnectionError("acknowledgement lost after the SM accepted")
        return result


@pytest.fixture
def lost_ack(monkeypatch):
    LostAck.lose = 1
    LostAck.sent = []
    monkeypatch.setattr(agent_runtime, "SMClient", LostAck)
    monkeypatch.setattr(approvals_api, "SMClient", LostAck)
    yield LostAck


async def _awaiting(estate) -> str:
    created = await agent_runtime.evaluate_agents(estate.state, TENANT)
    assert len(created) == 1, created
    return created[0].id


# ---------------------------------------------------------------------------
# 1. Lost acknowledgement -- the Site Manager accepted, Central Command
#    never heard
# ---------------------------------------------------------------------------


class TestLostAck:
    async def test_human_approval_lost_ack_retry_returns_the_same_directive(
        self, estate, lost_ack,
    ):
        pid = await _awaiting(estate)
        async with estate.client() as c:
            res = await c.post(f"/api/approvals/{pid}/approve")
        assert res.status_code == 200, res.text
        assert res.json()["delivery"]["delivered"] is False
        first = await estate.directives_for(pid)
        assert len(first) == 1, "the SM accepted the first call"
        row = await estate.proposal(pid)
        assert row.status == "approved" and row.directive_id == ""
        # The failed call released its own claim, so the next pass sends.
        assert row.dispatch_claimed_at is None

        dispatched = await agent_runtime.dispatch_decided(estate.state, TENANT)

        assert [p.id for p in dispatched] == [pid]
        after = await estate.directives_for(pid)
        assert [d.id for d in after] == [first[0].id], "a second directive"
        assert after[0].idempotency_key == idempotency_key(pid)
        row = await estate.proposal(pid)
        assert (row.status, row.directive_id) == ("dispatched", first[0].id)
        assert LostAck.sent[-1]["reason"] == REPLAY_REASON
        event = (await estate.cc_audit("agent_proposal.dispatched"))[-1]
        assert event.detail["idempotent_replay"] is True
        assert event.detail["directive_id"] == first[0].id
        assert len(await estate.sm_audit("directive.replayed")) == 1
        assert len(await estate.sm_audit("directive.enqueue")) == 1

    async def test_background_lost_ack_retry_returns_the_same_directive(
        self, autonomous_estate, lost_ack,
    ):
        estate = autonomous_estate
        stats = await agent_runtime.run_once(estate.state, TENANT)
        assert stats["autonomous"] == 1 and stats["dispatched"] == 0
        pid = (await estate.only_proposal()).id
        first = await estate.directives_for(pid)
        assert len(first) == 1
        assert first[0].authorization_basis == "autonomous_grant"

        stats = await agent_runtime.run_once(estate.state, TENANT)

        assert stats["dispatched"] == 1
        assert [d.id for d in await estate.directives_for(pid)] == [first[0].id]
        row = await estate.proposal(pid)
        assert (row.status, row.directive_id) == ("dispatched", first[0].id)
        assert await estate.all_directives() == 1

    async def test_a_second_and_third_loss_still_make_one_directive(
        self, estate, lost_ack,
    ):
        LostAck.lose = 3
        pid = await _awaiting(estate)
        async with estate.client() as c:
            await c.post(f"/api/approvals/{pid}/approve")
        for _ in range(3):
            await agent_runtime.dispatch_decided(estate.state, TENANT)
        assert len(LostAck.sent) == 4
        assert len({r["directive_id"] for r in LostAck.sent}) == 1
        assert len(await estate.directives_for(pid)) == 1
        assert (await estate.proposal(pid)).status == "dispatched"


# ---------------------------------------------------------------------------
# 2. Two dispatchers -- replicas, passes, the approval request
# ---------------------------------------------------------------------------


class TestOneClaimant:
    async def test_the_claim_has_exactly_one_winner(self, estate):
        pid = await _awaiting(estate)
        async with estate.cc() as session:
            row = await AgentProposalRepo(session).get(TENANT, pid)
            row.status = "approved"
            await session.commit()
        wins = []
        for _ in range(3):           # three replicas, one after another
            async with estate.cc() as session:
                repo = AgentProposalRepo(session)
                wins.append(await repo.claim_dispatch(await repo.get(TENANT, pid)))
                await session.commit()
        assert wins == [True, False, False]

    async def test_the_claim_is_only_ever_on_an_approved_proposal_as_judged(
        self, estate,
    ):
        """The conditional UPDATE re-asserts what the caller's gates judged:
        still `approved`, still on the same basis. A row that moved on --
        dispatched, failed, returned to the human queue, its basis
        rewritten -- is never claimed on a stale copy."""
        pid = await _awaiting(estate)
        async with estate.cc() as session:
            repo = AgentProposalRepo(session)
            stale = await repo.get(TENANT, pid)
        for status, basis in (
            ("awaiting_approval", "human_approval"),
            ("dispatched", "human_approval"),
            ("failed", "human_approval"),
            ("approved", "autonomous_grant"),     # basis moved under us
        ):
            async with estate.cc() as session:
                await session.execute(
                    update(CCAgentProposal).where(CCAgentProposal.id == pid)
                    .values(status=status, authorization_basis=basis,
                            dispatch_claimed_at=None))
                await session.commit()
            async with estate.cc() as session:
                stale.authorization_basis = "human_approval"
                assert await AgentProposalRepo(session).claim_dispatch(stale) is False, (
                    status, basis)
                await session.commit()

    async def test_a_held_claim_stops_another_pass_from_sending(
        self, estate, monkeypatch,
    ):
        """A replica has claimed and is mid-call: another pass must not
        send, withhold or fail the proposal."""
        pid = await _awaiting(estate)
        async with estate.cc() as session:
            repo = AgentProposalRepo(session)
            row = await repo.get(TENANT, pid)
            row.status = "approved"
            row.authorization_basis = "human_approval"
            await session.flush()
            assert await repo.claim_dispatch(row)
            await session.commit()
        sent = []

        class Spy(SMClient):
            async def dispatch_action(self, *a, **kw):
                sent.append(kw)
                return await super().dispatch_action(*a, **kw)

        monkeypatch.setattr(agent_runtime, "SMClient", Spy)
        assert await agent_runtime.dispatch_decided(estate.state, TENANT) == []
        assert sent == []
        assert (await estate.proposal(pid)).status == "approved"
        assert await estate.all_directives() == 0

    async def test_the_claim_is_committed_before_the_site_manager_is_called(
        self, estate, monkeypatch,
    ):
        """Read from a SEPARATE session at the moment of the call: the claim
        must already be durable, or a crash mid-call would leave nothing to
        stop another dispatcher."""
        pid = await _awaiting(estate)
        seen = []

        class Witness(SMClient):
            async def dispatch_action(self, *a, **kw):
                async with estate.cc() as other:
                    row = await AgentProposalRepo(other).get(TENANT, kw["proposal_id"])
                    seen.append((row.status, row.dispatch_claimed_at is not None))
                assert kw["timeout"] == agent_runtime.DISPATCH_RPC_TIMEOUT_S
                return await super().dispatch_action(*a, **kw)

        monkeypatch.setattr(agent_runtime, "SMClient", Witness)
        monkeypatch.setattr(approvals_api, "SMClient", Witness)
        async with estate.client() as c:
            res = await c.post(f"/api/approvals/{pid}/approve")
        assert res.json()["delivery"]["delivered"] is True
        assert seen == [("approved", True)]


# ---------------------------------------------------------------------------
# 3. Two approvers
# ---------------------------------------------------------------------------


class TestTwoApprovers:
    async def test_the_second_approver_reads_the_first_decision(self, estate):
        pid = await _awaiting(estate)
        async with estate.client() as c:
            estate.act_as("owner")
            first = await c.post(f"/api/approvals/{pid}/approve")
            estate.act_as("approver-2")
            second = await c.post(f"/api/approvals/{pid}/approve")
        assert first.json()["delivery"]["delivered"] is True
        assert second.status_code == 409
        assert len(await estate.directives_for(pid)) == 1

    async def test_the_decision_read_takes_a_row_lock(self):
        """The lock is what serializes two approvers on PostgreSQL (proven
        there); here, that the read asks for it and refreshes the row."""
        from sqlalchemy.dialects import postgresql

        captured = {}

        class Session:
            async def execute(self, stmt):
                captured["sql"] = str(stmt.compile(dialect=postgresql.dialect()))
                captured["opts"] = stmt.get_execution_options()

                class R:
                    def scalar_one_or_none(self):
                        return None
                return R()

        await AgentProposalRepo(Session()).get_for_decision(TENANT, "p")
        assert "FOR UPDATE" in captured["sql"]
        assert captured["opts"].get("populate_existing") is True


# ---------------------------------------------------------------------------
# 4. Crash and restart
# ---------------------------------------------------------------------------


class TestRestart:
    async def _claim_then_crash(self, estate, *, sent: bool) -> str:
        """Claim committed; the process dies before (or after) the SM call
        and before recording anything."""
        pid = await _awaiting(estate)
        async with estate.cc() as session:
            repo = AgentProposalRepo(session)
            row = await repo.get(TENANT, pid)
            row.status = "approved"
            row.decided_by = "owner@example.com"
            await session.flush()
            assert await repo.claim_dispatch(row)
            await session.commit()
        if sent:
            ack = await SMClient("").dispatch_action(
                estate.sm_endpoint, TOKEN, tenant_id=TENANT,
                site_id=estate.site_id, device_agent_id="node-1",
                action_type="SEL_CLEAR", actor=row.actor,
                authorization="human_approval", proposal_id=pid,
            )
            assert ack["accepted"]
        return pid

    async def test_the_claim_survives_and_the_lease_holds_it(self, estate):
        pid = await self._claim_then_crash(estate, sent=False)
        # A fresh process (same database): the claim is still there.
        assert (await estate.proposal(pid)).dispatch_claimed_at is not None
        assert await agent_runtime.dispatch_decided(estate.state, TENANT) == []
        assert await estate.all_directives() == 0

    async def test_after_the_lease_the_resend_is_idempotent(self, estate):
        pid = await self._claim_then_crash(estate, sent=True)
        before = await estate.directives_for(pid)
        assert len(before) == 1
        await estate.age_claim(pid)
        dispatched = await agent_runtime.dispatch_decided(estate.state, TENANT)
        assert [p.id for p in dispatched] == [pid]
        assert [d.id for d in await estate.directives_for(pid)] == [before[0].id]
        row = await estate.proposal(pid)
        assert (row.status, row.directive_id) == ("dispatched", before[0].id)
        event = (await estate.cc_audit("agent_proposal.dispatched"))[-1]
        assert event.detail["idempotent_replay"] is True

    async def test_after_the_lease_a_never_sent_claim_is_sent_once(self, estate):
        pid = await self._claim_then_crash(estate, sent=False)
        await estate.age_claim(pid)
        assert len(await agent_runtime.dispatch_decided(estate.state, TENANT)) == 1
        assert len(await estate.directives_for(pid)) == 1
        event = (await estate.cc_audit("agent_proposal.dispatched"))[-1]
        assert event.detail["idempotent_replay"] is False

    async def test_release_never_clears_a_claim_somebody_else_took(self, estate):
        pid = await self._claim_then_crash(estate, sent=False)
        async with estate.cc() as session:
            stale = await AgentProposalRepo(session).get(TENANT, pid)
        await estate.age_claim(pid)
        async with estate.cc() as session:
            repo = AgentProposalRepo(session)
            assert await repo.claim_dispatch(await repo.get(TENANT, pid))
            await session.commit()
        async with estate.cc() as session:
            # The crashed claimer comes back with its old copy.
            assert await AgentProposalRepo(session).release_claim(stale) is False
            await session.commit()
        assert (await estate.proposal(pid)).dispatch_claimed_at is not None


# ---------------------------------------------------------------------------
# 5. The Site Manager key
# ---------------------------------------------------------------------------


def _ack_call(endpoint, **kw):
    body = dict(
        tenant_id=TENANT, site_id="s1", device_agent_id="node-1",
        action_type="SEL_CLEAR", params_json="{}", actor="op-agent:a1@v1",
        authorization="human_approval", decided_by="op@example.com",
        proposal_id="prop-1",
    )
    body.update(kw)

    async def call():
        import grpc

        async with grpc.aio.insecure_channel(endpoint) as channel:
            stub = harkeniq_pb2_grpc.SiteManagerServiceStub(channel)
            return await stub.DispatchAction(
                harkeniq_pb2.ActionDispatch(**body),
                metadata=[("authorization", f"Bearer {TOKEN}")],
            )
    return call()


class TestTheSiteManagerKey:
    async def test_a_duplicate_dispatch_returns_the_existing_directive(self, estate):
        first = await _ack_call(estate.sm_endpoint)
        second = await _ack_call(estate.sm_endpoint)
        assert first.accepted and second.accepted
        assert second.directive_id == first.directive_id
        assert (first.reason, second.reason) == ("", REPLAY_REASON)
        rows = await estate.directives_for("prop-1")
        assert len(rows) == 1 and rows[0].idempotency_key == "proposal:prop-1"

    async def test_different_proposals_still_make_different_directives(self, estate):
        a = await _ack_call(estate.sm_endpoint, proposal_id="prop-a")
        b = await _ack_call(estate.sm_endpoint, proposal_id="prop-b")
        assert a.directive_id != b.directive_id
        assert await estate.all_directives() == 2

    async def test_a_dispatch_with_no_proposal_is_unkeyed_as_before(self, estate):
        """Campaign steps carry no proposal id (PB-F14 keeps its owner)."""
        a = await _ack_call(estate.sm_endpoint, proposal_id="")
        b = await _ack_call(estate.sm_endpoint, proposal_id="")
        assert a.directive_id != b.directive_id
        async with estate.sm() as session:
            keys = (await session.execute(
                select(DirectedDirective.idempotency_key))).scalars().all()
        assert keys == [None, None]

    async def test_one_key_never_answers_for_different_work(self, estate):
        first = await _ack_call(estate.sm_endpoint)
        other = await _ack_call(estate.sm_endpoint, action_type="IDENTIFY_LED")
        assert first.accepted and not other.accepted
        assert "different device or action" in other.reason
        assert other.directive_id == ""
        assert await estate.all_directives() == 1

    async def test_the_replay_precedes_the_safety_decision(self, estate):
        """The directive already exists; refusing the repeat now would make
        Central Command record a failure for work that will still be
        delivered. A NEW proposal is still refused by the halt."""
        first = await _ack_call(estate.sm_endpoint)
        estate.autonomy.activate_stop_switch("op")
        again = await _ack_call(estate.sm_endpoint)
        fresh = await _ack_call(estate.sm_endpoint, proposal_id="prop-new")
        assert again.accepted and again.directive_id == first.directive_id
        assert not fresh.accepted
        assert await estate.all_directives() == 1

    async def test_a_lost_insert_race_answers_with_the_winner(self, estate):
        """Two requests both pass the lookup; the unique index decides."""
        async with estate.sm() as session:
            device = await DeviceRepo(session).get_by_agent_id("node-1")
        key = idempotency_key("prop-race")
        kw = dict(idempotency_key=key, device_id=device.id,
                  action_type="SEL_CLEAR", proposal_id="prop-race")
        won, replayed_1 = await estate.directives.enqueue_action_once(**kw)
        lost, replayed_2 = await estate.directives.enqueue_action_once(**kw)
        assert (replayed_1, replayed_2) == (False, True)
        assert lost == won
        assert len(await estate.directives_for("prop-race")) == 1

    async def test_historical_unkeyed_directives_remain_valid(self, estate):
        """Directives written before PB-0 carry no key -- including the
        duplicates the old code made. Both still deliver and settle, and a
        keyed directive coexists with them."""
        async with estate.sm() as session:
            device = await DeviceRepo(session).get_by_agent_id("node-1")
            repo = DirectiveRepo(session)
            old = [await repo.enqueue(device_id=device.id, kind="action",
                                      action_type="SEL_CLEAR", proposal_id="legacy")
                   for _ in range(2)]
            await session.commit()
        await _ack_call(estate.sm_endpoint, proposal_id="new-one")
        delivered = await estate.directives.poll("node-1")
        assert {d.directive_id for d in delivered} >= {o.id for o in old}
        for directive in old:
            assert await estate.directives.report_result(
                "node-1", directive.id, True, "ok")
        async with estate.sm() as session:
            rows = {r.id: r for r in (await session.execute(
                select(DirectedDirective))).scalars().all()}
        assert all(rows[o.id].idempotency_key is None for o in old)
        assert all(rows[o.id].status == "completed" for o in old)


# ---------------------------------------------------------------------------
# 6. Ordering -- every approved proposal is visited
# ---------------------------------------------------------------------------


class TestKeysetSelection:
    async def _approved(self, estate, n, *, created, agent=None, key="k"):
        async with estate.cc() as session:
            ids = []
            for i in range(n):
                row = CCAgentProposal(
                    tenant_id=TENANT, agent_id=agent or "ghost-agent",
                    actor=f"op-agent:{agent or 'ghost-agent'}@v1", agent_version=1,
                    site_id=estate.site_id, device_agent_id="node-1",
                    action_type="SEL_CLEAR", params={}, rationale="r", evidence={},
                    disposition="requires_approval",
                    authorization_basis="human_approval", status="approved",
                    decided_by="owner@example.com", dedupe_key=f"{key}-{i}",
                    created_at=created(i),
                )
                session.add(row)
                await session.flush()
                ids.append(row.id)
            await session.commit()
            return ids

    async def test_six_hundred_withheld_proposals_starve_nobody(self, estate):
        """The oldest-500 read: 500 approved proposals that can never
        dispatch (their agent is gone) hid every newer one forever."""
        t0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
        await self._approved(estate, 600, created=lambda i: t0 + timedelta(seconds=i))
        pid = await _awaiting(estate)
        # Approve it, but let the background pass be the one to send.
        async with estate.cc() as session:
            row = await AgentProposalRepo(session).get(TENANT, pid)
            row.status, row.decided_by = "approved", "owner@example.com"
            await session.commit()
        dispatched = await agent_runtime.dispatch_decided(estate.state, TENANT)
        assert [p.id for p in dispatched] == [pid]
        assert len(await estate.directives_for(pid)) == 1

    async def test_equal_created_at_pages_by_id_and_visits_each_once(
        self, estate, monkeypatch,
    ):
        monkeypatch.setattr(agent_runtime, "DISPATCH_PAGE", 3)
        same = datetime(2026, 9, 1, tzinfo=timezone.utc)
        ids = await self._approved(estate, 10, created=lambda i: same)
        visited = []
        real = agent_runtime.revalidate_dispatch

        async def spy(session, tenant_id, proposal, **kw):
            visited.append(proposal.id)
            return await real(session, tenant_id, proposal, **kw)

        monkeypatch.setattr(agent_runtime, "revalidate_dispatch", spy)
        await agent_runtime.dispatch_decided(estate.state, TENANT)
        assert visited == sorted(ids), "every proposal, once, in (created_at, id)"

    async def test_twenty_reruns_visit_in_the_same_order(self, estate, monkeypatch):
        monkeypatch.setattr(agent_runtime, "DISPATCH_PAGE", 4)
        t0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
        await self._approved(estate, 9, created=lambda i: t0 + timedelta(seconds=i % 3))
        orders = []
        real = agent_runtime.revalidate_dispatch
        for _ in range(20):
            visited = []

            async def spy(session, tenant_id, proposal, **kw):
                visited.append(proposal.id)
                return await real(session, tenant_id, proposal, **kw)

            monkeypatch.setattr(agent_runtime, "revalidate_dispatch", spy)
            await agent_runtime.dispatch_decided(estate.state, TENANT)
            orders.append(tuple(visited))
        assert len(set(orders)) == 1 and len(orders[0]) == 9


# ---------------------------------------------------------------------------
# 7. Claim ownership on the background path
# ---------------------------------------------------------------------------


class TestOwnership:
    async def test_a_withheld_proposal_returns_with_its_claim_released(
        self, autonomous_estate, monkeypatch,
    ):
        estate = autonomous_estate

        async def exhausted(session, tenant_id, proposal):
            return False, "the agent's execution budget is spent"

        monkeypatch.setattr(agent_runtime, "_unattended_allowed", exhausted)
        await agent_runtime.run_once(estate.state, TENANT)
        row = await estate.only_proposal()
        assert row.status == "awaiting_approval"
        assert row.dispatch_claimed_at is None, "a fresh human decision waits on nothing"
        async with estate.client() as c:
            res = await c.post(f"/api/approvals/{row.id}/approve")
        assert res.json()["delivery"]["delivered"] is True
        assert len(await estate.directives_for(row.id)) == 1

    async def test_a_gate_refusal_takes_no_claim(self, estate):
        """Gates BEFORE the claim: nothing is held for work that may not run."""
        pid = await _awaiting(estate)
        async with estate.cc() as session:
            row = await AgentProposalRepo(session).get(TENANT, pid)
            row.status, row.decided_by = "approved", "owner@example.com"
            row.agent_id = "gone"
            row.actor = "op-agent:gone@v1"
            await session.commit()
        await agent_runtime.dispatch_decided(estate.state, TENANT)
        row = await estate.proposal(pid)
        assert row.status == "approved" and row.dispatch_claimed_at is None
        assert "no longer exists" in row.dispatch_reason
        # A second pass does not audit the same cause again.
        await agent_runtime.dispatch_decided(estate.state, TENANT)
        assert len(await estate.cc_audit("agent_proposal.dispatch_withheld")) == 1


# ---------------------------------------------------------------------------
# 8. Structure
# ---------------------------------------------------------------------------


def _calls(name: str) -> set[str]:
    found: set[str] = set()
    for path in CC_SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for call in ast.walk(node):
                if isinstance(call, ast.Call):
                    f = call.func
                    called = (f.attr if isinstance(f, ast.Attribute)
                              else f.id if isinstance(f, ast.Name) else "")
                    if called == name:
                        found.add(f"{path.name}:{node.name}")
    return found


class TestStructure:
    def test_one_claim_site_per_path(self):
        assert _calls("claim_dispatch") == {
            "agent_runtime.py:_dispatch_one",
            "approvals.py:_decide_agent_proposal",
        }

    def test_one_proposal_sender_and_it_carries_a_deadline_inside_the_lease(self):
        assert _calls("_send_claimed") == {
            "agent_runtime.py:_dispatch_one",
            "approvals.py:_decide_agent_proposal",
        }
        lease = AgentProposalRepo.DISPATCH_CLAIM_LEASE.total_seconds()
        assert 0 < agent_runtime.DISPATCH_RPC_TIMEOUT_S < lease
        assert lease < 120, "a crashed claim is re-sent on the next pass"

    def test_the_oldest_500_read_is_gone(self):
        assert not hasattr(AgentProposalRepo, "list_by_status")
        assert _calls("list_by_status") == set()

    def test_the_key_and_the_replay_signal_are_defined_once(self):
        assert idempotency_key("abc") == f"{KEY_PREFIX}abc" == "proposal:abc"
        assert idempotency_key("") is None and idempotency_key("  ") is None
        assert is_replay({"accepted": True, "reason": REPLAY_REASON})
        assert not is_replay({"accepted": False, "reason": REPLAY_REASON})
        assert not is_replay({"accepted": True, "reason": ""})
        sm = (REPO / "services/site_manager/src/harkeniq_sm/grpc_server.py").read_text()
        assert '"idempotent_replay"' not in sm and "'proposal:'" not in sm
        assert "REPLAY_REASON" in sm and "idempotency_key(" in sm

    def test_the_gates_are_untouched(self):
        from harkeniq_cc.agent_activation import DISPATCH_GATES

        assert "dispatch_claim" not in DISPATCH_GATES
        assert len(DISPATCH_GATES) == 10
