"""PB-0 (A30.42/A30.43) on a REAL PostgreSQL: one proposal, one directive.

The sqlite proofs live in tests/unit/cc/test_pb0_dispatch_idempotency.py.
sqlite shares one connection between sessions, so it cannot race. This
file is where the races are real: two Central Command "replicas" (two
engines, two connection pools) racing the claim; two approvers racing the
decision's row lock; ten concurrent `DispatchAction` calls racing the Site
Manager's unique index; a claim surviving a process restart and expiring by
`timestamptz` arithmetic; the keyset walk over more than a page of approved
proposals with IDENTICAL `created_at`; and the 0028 -> 0029 / 0011 -> 0012
upgrades on databases holding rows -- including duplicate directives the
old code made -- and an index left INVALID by an interrupted build.

Each test owns a tenant (CC) or a tag (SM) on the shared, migrated
database. Audit rows are deliberately NOT cleaned up: the chain is
append-only. Gated on ``HARKEN_TEST_CC_PG_DSN`` and, for the Site Manager
half, ``HARKEN_TEST_SM_PG_DSN``.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import grpc
import httpx
import pytest
import sqlalchemy as sa

from harkeniq.dispatch_idempotency import REPLAY_REASON, idempotency_key
from harkeniq.proto import harkeniq_pb2, harkeniq_pb2_grpc
from harkeniq_cc import agent_runtime
from harkeniq_cc.api.deps import get_current_user
from harkeniq_cc.app import create_app
from harkeniq_cc.auth import ROLE_PERMISSIONS, UserContext, configure_auth
from harkeniq_cc.config import CCConfig
from harkeniq_cc.db.base import make_engine, make_sessionmaker
from harkeniq_cc.db.models import (
    CCAgentCapability,
    CCAgentProposal,
    CCFleetCache,
    CCIncident,
    CCOperationalAgent,
    CCScopeGrant,
    CCSite,
)
from harkeniq_cc.db.repos import AgentProposalRepo
from harkeniq_cc.runtime import AppState

from tests.unit.cc.conftest import seed_tenant_admin, seed_tenant_people

CC_DSN = os.environ.get("HARKEN_TEST_CC_PG_DSN", "")
SM_DSN = os.environ.get("HARKEN_TEST_SM_PG_DSN", "")
REPO = Path(__file__).resolve().parents[2]
TOKEN = "pb0-site-token"

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not CC_DSN, reason="HARKEN_TEST_CC_PG_DSN not set"),
]
needs_sm = pytest.mark.skipif(not SM_DSN, reason="HARKEN_TEST_SM_PG_DSN not set")


# ---------------------------------------------------------------------------
# Central Command estate on PostgreSQL
# ---------------------------------------------------------------------------


class CountingSM:
    """Counts every call that reaches the Site Manager verb. Slow, so two
    dispatchers that both got past the claim would overlap -- and be seen."""

    calls: list = []
    delay = 0.2

    def __init__(self, *_a, **_kw):
        pass

    async def dispatch_action(self, endpoint, token, **kw):
        CountingSM.calls.append(kw)
        await asyncio.sleep(CountingSM.delay)
        return {"accepted": True, "directive_id": f"dir-{kw['proposal_id']}",
                "reason": ""}


@pytest.fixture
def counting_sm(monkeypatch):
    CountingSM.calls = []
    monkeypatch.setattr("harkeniq_cc.agent_runtime.SMClient", CountingSM)
    monkeypatch.setattr("harkeniq_cc.api.approvals.SMClient", CountingSM)
    yield CountingSM


async def _estate(sessionmaker, tenant: str, *, sm_endpoint="sm:50051",
                  node: str | None = None) -> tuple[str, str, str]:
    """One site, one device with an open disk incident, one agent bound to
    IDENTIFY_LED with a site grant. Returns (agent_id, site_id, node)."""
    caps = {"reach_known": True, "implemented": ["IDENTIFY_LED"],
            "allowed": ["IDENTIFY_LED"], "effective": ["IDENTIFY_LED"]}
    node = node or f"node-{uuid.uuid4().hex[:8]}"
    async with sessionmaker() as session:
        site = CCSite(tenant_id=tenant, site_name=f"pb0-{uuid.uuid4().hex[:6]}",
                      sm_endpoint=sm_endpoint, sm_token=TOKEN)
        session.add(site)
        await session.flush()
        session.add_all([
            CCFleetCache(
                site_id=site.id, agent_id=node, agent_name="pb0-node",
                vendor="Dell", model="R750", device_class="server",
                observation="observed", health="Critical", capabilities=caps,
            ),
            CCIncident(
                incident_id=f"inc-{uuid.uuid4().hex[:8]}", tenant_id=tenant,
                site_id=site.id, kind="device", status="open",
                title="disk failing", device_agent_id=node, subsystem="disk",
                confidence=0.9,
                components=[{"component": "Disk.Bay.1", "severity": "CRITICAL"}],
            ),
        ])
        agent = CCOperationalAgent(
            tenant_id=tenant, name=f"pb0-agent-{uuid.uuid4().hex[:6]}",
            status="active", version=1, activated_version=1,
            created_by="pb0-test", autonomy_ceiling=0,
        )
        session.add(agent)
        await session.flush()
        session.add_all([
            CCScopeGrant(
                tenant_id=tenant, principal_type="agent", principal_ref=agent.id,
                scope_type="site", scope_ref=site.id, granted_by="pb0-test",
            ),
            CCAgentCapability(
                agent_id=agent.id, tenant_id=tenant, kind="action_class",
                capability_ref="IDENTIFY_LED",
            ),
        ])
        await session.commit()
        return agent.id, site.id, node


def _app(engine, sessionmaker, tenant: str, user: str):
    config = CCConfig(tenant_id=tenant, insecure=True)
    configure_auth("", "", "", insecure=True)
    state = AppState(config=config, engine=engine, sessionmaker=sessionmaker)
    app = create_app(state)

    async def _fake():
        return UserContext(
            user_id=f"kc-{user}", email=f"{user}@example.com", tenant_id=tenant,
            role="tenant_owner",
            permissions=list(ROLE_PERMISSIONS["tenant_owner"]),
        )

    app.dependency_overrides[get_current_user] = _fake
    return app, state


async def _approve_directly(sessionmaker, tenant: str, pid: str):
    async with sessionmaker() as session:
        row = await AgentProposalRepo(session).get(tenant, pid)
        row.status, row.decided_by = "approved", "owner@example.com"
        await session.commit()


async def _row(sessionmaker, tenant: str, pid: str) -> CCAgentProposal:
    async with sessionmaker() as session:
        return await AgentProposalRepo(session).get(tenant, pid)


async def _cleanup(sessionmaker, tenant: str):
    async with sessionmaker() as session:
        for stmt in (
            "DELETE FROM cc_fleet_cache WHERE site_id IN "
            "(SELECT id FROM cc_sites WHERE tenant_id = :t)",
            "DELETE FROM cc_approval_records WHERE subject_ref IN "
            "(SELECT id FROM cc_agent_proposals WHERE tenant_id = :t)",
            "DELETE FROM cc_agent_proposals WHERE tenant_id = :t",
            "DELETE FROM cc_incidents WHERE tenant_id = :t",
            "DELETE FROM cc_agent_capabilities WHERE tenant_id = :t",
            "DELETE FROM cc_scope_grants WHERE tenant_id = :t",
            "DELETE FROM cc_operational_agents WHERE tenant_id = :t",
            "DELETE FROM cc_sites WHERE tenant_id = :t",
        ):
            try:
                await session.execute(sa.text(stmt), {"t": tenant})
                await session.commit()
            except Exception:  # noqa: BLE001 - best effort on a shared db
                await session.rollback()


def _barrier_around(monkeypatch, target, name: str, parties: int):
    barrier = asyncio.Barrier(parties)
    real = getattr(target, name)

    async def wrapped(*a, **kw):
        await barrier.wait()
        return await real(*a, **kw)

    monkeypatch.setattr(target, name, wrapped)


# ---------------------------------------------------------------------------
# 1. Two replicas race the claim
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("round_", range(5))
async def test_two_replicas_race_and_one_wins(round_, counting_sm, monkeypatch):
    tenant = f"pb0r-{uuid.uuid4().hex[:8]}"
    engines = [make_engine(CC_DSN), make_engine(CC_DSN)]
    makers = [make_sessionmaker(e) for e in engines]
    try:
        apps = [_app(e, m, tenant, "owner") for e, m in zip(engines, makers)]
        await seed_tenant_admin(makers[0], tenant, "kc-owner")
        await _estate(makers[0], tenant)
        created = await agent_runtime.evaluate_agents(apps[0][1], tenant)
        assert len(created) == 1
        pid = created[0].id
        await _approve_directly(makers[0], tenant, pid)
        # Both replicas clear the gate at the same instant, then race.
        _barrier_around(monkeypatch, agent_runtime, "revalidate_dispatch", 2)

        results = await asyncio.gather(*(
            agent_runtime.dispatch_decided(state, tenant) for _app_, state in apps
        ))

        assert sorted(len(r) for r in results) == [0, 1], results
        assert len(CountingSM.calls) == 1, "both replicas reached the Site Manager"
        row = await _row(makers[0], tenant, pid)
        assert (row.status, row.directive_id) == ("dispatched", f"dir-{pid}")
        assert row.dispatch_claimed_at is not None
        assert row.dispatch_claimed_at.tzinfo is not None
    finally:
        await _cleanup(makers[0], tenant)
        for e in engines:
            await e.dispose()


# ---------------------------------------------------------------------------
# 2. Two approvers finish at once
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("round_", range(5))
async def test_two_approvers_finish_together_and_one_directive_follows(
    round_, counting_sm, monkeypatch,
):
    tenant = f"pb0a-{uuid.uuid4().hex[:8]}"
    engines = [make_engine(CC_DSN), make_engine(CC_DSN)]
    makers = [make_sessionmaker(e) for e in engines]
    try:
        await seed_tenant_people(
            makers[0], tenant, [("kc-alice", "tenant_owner"), ("kc-bob", "tenant_owner")],
        )
        apps = [
            _app(engines[0], makers[0], tenant, "alice"),
            _app(engines[1], makers[1], tenant, "bob"),
        ]
        await _estate(makers[0], tenant)
        created = await agent_runtime.evaluate_agents(apps[0][1], tenant)
        pid = created[0].id
        assert created[0].status == "awaiting_approval"
        # Both requests reach the locked read at the same instant.
        _barrier_around(monkeypatch, AgentProposalRepo, "get_for_decision", 2)

        async def approve(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test",
            ) as c:
                return await c.post(f"/api/approvals/{pid}/approve")

        responses = await asyncio.gather(*(approve(app) for app, _s in apps))

        codes = sorted(r.status_code for r in responses)
        assert codes == [200, 409], [r.text for r in responses]
        winner = next(r for r in responses if r.status_code == 200)
        assert winner.json()["delivery"]["delivered"] is True
        assert len(CountingSM.calls) == 1
        assert (await _row(makers[0], tenant, pid)).status == "dispatched"
        async with makers[0]() as session:
            records = (await session.execute(sa.text(
                "SELECT count(*) FROM cc_approval_records WHERE subject_ref = :p"),
                {"p": pid})).scalar()
        # The loser read the winner's committed decision, exactly as a
        # sequential second approver would, and recorded nothing.
        assert records == 1
    finally:
        await _cleanup(makers[0], tenant)
        for e in engines:
            await e.dispose()


@pytest.mark.parametrize("path", ["sync", "background"])
async def test_during_the_call_the_claim_is_committed_and_nothing_is_locked(
    path, monkeypatch,
):
    """A30.42: the claim is committed BEFORE the Site Manager call and no
    lock is held across it. Asked from ANOTHER connection, mid-call: the
    claim is visible, and the row can be locked with NOWAIT."""
    tenant = f"pb0w-{uuid.uuid4().hex[:8]}"
    engine = make_engine(CC_DSN)
    maker = make_sessionmaker(engine)
    witness = make_engine(CC_DSN)
    seen = []

    class Witness:
        def __init__(self, *_a, **_kw):
            pass

        async def dispatch_action(self, endpoint, token, **kw):
            async with witness.connect() as conn:
                row = (await conn.execute(sa.text(
                    "SELECT status, dispatch_claimed_at FROM cc_agent_proposals "
                    "WHERE id = :p FOR UPDATE NOWAIT"), {"p": kw["proposal_id"]})).one()
                await conn.rollback()
            seen.append((row[0], row[1] is not None))
            return {"accepted": True, "directive_id": "dir-w", "reason": ""}

    monkeypatch.setattr("harkeniq_cc.agent_runtime.SMClient", Witness)
    monkeypatch.setattr("harkeniq_cc.api.approvals.SMClient", Witness)
    try:
        app, state = _app(engine, maker, tenant, "owner")
        await seed_tenant_admin(maker, tenant, "kc-owner")
        await _estate(maker, tenant)
        pid = (await agent_runtime.evaluate_agents(state, tenant))[0].id
        if path == "sync":
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test",
            ) as c:
                res = await c.post(f"/api/approvals/{pid}/approve")
            assert res.json()["delivery"]["delivered"] is True
        else:
            await _approve_directly(maker, tenant, pid)
            assert len(await agent_runtime.dispatch_decided(state, tenant)) == 1
        assert seen == [("approved", True)]
    finally:
        await _cleanup(maker, tenant)
        await engine.dispose()
        await witness.dispose()


# ---------------------------------------------------------------------------
# 3. Restart: the claim is durable and its lease is timestamptz arithmetic
# ---------------------------------------------------------------------------


async def test_a_claim_survives_a_restart_and_expires_by_its_lease(counting_sm):
    tenant = f"pb0c-{uuid.uuid4().hex[:8]}"
    engine = make_engine(CC_DSN)
    maker = make_sessionmaker(engine)
    try:
        _app_, state = _app(engine, maker, tenant, "owner")
        await seed_tenant_admin(maker, tenant, "kc-owner")
        await _estate(maker, tenant)
        pid = (await agent_runtime.evaluate_agents(state, tenant))[0].id
        await _approve_directly(maker, tenant, pid)
        async with maker() as session:
            repo = AgentProposalRepo(session)
            assert await repo.claim_dispatch(await repo.get(tenant, pid))
            await session.commit()
        await engine.dispose()                         # the process dies

        engine = make_engine(CC_DSN)                   # and comes back
        maker = make_sessionmaker(engine)
        _app_, state = _app(engine, maker, tenant, "owner")
        assert (await _row(maker, tenant, pid)).dispatch_claimed_at is not None
        assert await agent_runtime.dispatch_decided(state, tenant) == []

        async def claimed_ago(seconds: int):
            async with maker() as session:
                await session.execute(sa.text(
                    "UPDATE cc_agent_proposals SET dispatch_claimed_at = "
                    "now() - make_interval(secs => :s) WHERE id = :p"),
                    {"s": seconds, "p": pid})
                await session.commit()

        await claimed_ago(55)
        assert await agent_runtime.dispatch_decided(state, tenant) == []
        assert CountingSM.calls == []
        await claimed_ago(65)
        assert len(await agent_runtime.dispatch_decided(state, tenant)) == 1
        assert len(CountingSM.calls) == 1
        assert CountingSM.calls[0]["proposal_id"] == pid
    finally:
        await _cleanup(maker, tenant)
        await engine.dispose()


# ---------------------------------------------------------------------------
# 4. Ordering: more than a page, identical created_at, every row once
# ---------------------------------------------------------------------------


async def test_every_approved_proposal_is_visited_once_in_keyset_order(
    counting_sm, monkeypatch,
):
    tenant = f"pb0k-{uuid.uuid4().hex[:8]}"
    engine = make_engine(CC_DSN)
    maker = make_sessionmaker(engine)
    try:
        _app_, state = _app(engine, maker, tenant, "owner")
        await seed_tenant_admin(maker, tenant, "kc-owner")
        _agent, site_id, node = await _estate(maker, tenant)
        live = (await agent_runtime.evaluate_agents(state, tenant))[0].id
        same = datetime(2026, 9, 1, tzinfo=timezone.utc)
        async with maker() as session:
            session.add_all([
                CCAgentProposal(
                    tenant_id=tenant, agent_id="ghost", actor="op-agent:ghost@v1",
                    agent_version=1, site_id=site_id, device_agent_id=node,
                    action_type="IDENTIFY_LED", params={}, rationale="r",
                    evidence={}, disposition="requires_approval",
                    authorization_basis="human_approval", status="approved",
                    decided_by="owner@example.com", dedupe_key=f"ghost-{i}",
                    created_at=same,
                ) for i in range(1100)
            ])
            await session.commit()
            ghosts = sorted((await session.execute(sa.text(
                "SELECT id FROM cc_agent_proposals WHERE tenant_id = :t "
                "AND agent_id = 'ghost'"), {"t": tenant})).scalars().all())
        await _approve_directly(maker, tenant, live)
        visited = []
        real = agent_runtime.revalidate_dispatch

        async def spy(session, tenant_id, proposal, **kw):
            visited.append(proposal.id)
            return await real(session, tenant_id, proposal, **kw)

        monkeypatch.setattr(agent_runtime, "revalidate_dispatch", spy)
        dispatched = await agent_runtime.dispatch_decided(state, tenant)

        assert [p.id for p in dispatched] == [live]
        assert visited[:1100] == ghosts, "identical created_at pages by id"
        assert visited[1100:] == [live] and len(visited) == 1101
    finally:
        await _cleanup(maker, tenant)
        await engine.dispose()


# ---------------------------------------------------------------------------
# 5. The Site Manager on PostgreSQL: the unique index decides
# ---------------------------------------------------------------------------


class _SM:
    """A served Site Manager on the shared PostgreSQL, with a tagged site."""

    async def start(self):
        from harkeniq_sm.approvals import ApprovalService
        from harkeniq_sm.autonomy import SMAutonomyEnforcer
        from harkeniq_sm.config import SMConfig
        from harkeniq_sm.db import base as sm_base
        from harkeniq_sm.db.repos import DeviceRepo, SiteRepo
        from harkeniq_sm.directives import DirectiveService
        from harkeniq_sm.grpc_server import (
            AgentServiceServicer, SiteManagerServiceServicer, build_server,
        )
        from harkeniq_sm.ingest import IngestService

        self.tag = uuid.uuid4().hex[:8]
        self.engine = sm_base.make_engine(SM_DSN)
        self.db = sm_base.make_sessionmaker(self.engine)
        config = SMConfig(insecure=True, site_name=f"pb0-{self.tag}",
                          grpc_port=0, site_token=TOKEN)
        self.directives = DirectiveService(self.db, config)
        servicer = SiteManagerServiceServicer(
            self.db, ApprovalService(self.db, config), config,
            directives=self.directives, autonomy=SMAutonomyEnforcer(),
        )
        self.server, port = build_server(
            config, AgentServiceServicer(IngestService(self.db, config)),
            sm_servicer=servicer,
        )
        await self.server.start()
        self.endpoint = f"localhost:{port}"
        self.node = f"pb0-node-{self.tag}"
        async with self.db() as session:
            site = await SiteRepo(session).get_or_create(f"pb0-{self.tag}")
            site.cc_site_id = f"pb0-cc-{self.tag}"
            device = await DeviceRepo(session).upsert_registration(
                site_id=site.id, agent_id=self.node, vendor="dell",
            )
            await session.commit()
            self.site_id, self.device_id = site.id, device.id
        return self

    async def directives_for(self, proposal_id: str) -> list:
        async with self.db() as session:
            return (await session.execute(sa.text(
                "SELECT id, idempotency_key FROM sm_directives "
                "WHERE proposal_id = :p ORDER BY created_at"),
                {"p": proposal_id})).all()

    async def call(self, **kw):
        body = dict(
            tenant_id="t1", site_id="s1", device_agent_id=self.node,
            action_type="SEL_CLEAR", params_json="{}", actor="op-agent:a@v1",
            authorization="human_approval", decided_by="op@example.com",
            proposal_id=f"pb0-prop-{self.tag}",
        )
        body.update(kw)
        async with grpc.aio.insecure_channel(self.endpoint) as channel:
            stub = harkeniq_pb2_grpc.SiteManagerServiceStub(channel)
            return await stub.DispatchAction(
                harkeniq_pb2.ActionDispatch(**body),
                metadata=[("authorization", f"Bearer {TOKEN}")],
            )

    async def close(self):
        await self.server.stop(None)
        async with self.db() as session:
            for stmt in (
                "DELETE FROM sm_directives WHERE device_id = :d",
                "DELETE FROM devices WHERE id = :d",
            ):
                await session.execute(sa.text(stmt), {"d": self.device_id})
            await session.execute(
                sa.text("DELETE FROM sites WHERE id = :s"), {"s": self.site_id})
            await session.commit()
        await self.engine.dispose()


@needs_sm
async def test_ten_concurrent_dispatches_of_one_proposal_make_one_directive():
    # Rounds inside ONE event loop: the Site Manager's in-process audit lock
    # is a module-level asyncio.Lock, bound to the first loop that contends
    # for it (one loop in production).
    sm = await _SM().start()
    try:
        for round_ in range(3):
            proposal = f"pb0-prop-{sm.tag}-{round_}"
            acks = await asyncio.gather(*(
                sm.call(proposal_id=proposal) for _ in range(10)))
            assert all(a.accepted for a in acks), [a.reason for a in acks]
            assert len({a.directive_id for a in acks}) == 1
            assert sorted(a.reason for a in acks) == [""] + [REPLAY_REASON] * 9
            rows = await sm.directives_for(proposal)
            assert len(rows) == 1
            assert rows[0][1] == idempotency_key(proposal)
    finally:
        await sm.close()


@needs_sm
async def test_the_unique_index_alone_decides_ten_concurrent_inserts():
    """No lookup in front: ten inserts under one key race the index itself.
    One wins; nine lose the unique constraint and are answered with the
    winner's directive."""
    sm = await _SM().start()
    try:
        key = idempotency_key(f"pb0-race-{sm.tag}")
        results = await asyncio.gather(*(
            sm.directives.enqueue_action_once(
                idempotency_key=key, device_id=sm.device_id,
                action_type="SEL_CLEAR", proposal_id=f"pb0-race-{sm.tag}",
            ) for _ in range(10)
        ))
        assert sorted(replayed for _id, replayed in results) == [False] + [True] * 9
        assert len({directive_id for directive_id, _r in results}) == 1
        assert len(await sm.directives_for(f"pb0-race-{sm.tag}")) == 1
    finally:
        await sm.close()


@needs_sm
async def test_a_lost_ack_on_postgres_end_to_end(monkeypatch):
    """CC on PostgreSQL, a real Site Manager on PostgreSQL: the SM accepts,
    the acknowledgement is lost, the next pass re-sends, one directive."""
    from harkeniq_cc.sm_client import SMClient

    sm = await _SM().start()
    tenant = f"pb0l-{uuid.uuid4().hex[:8]}"
    engine = make_engine(CC_DSN)
    maker = make_sessionmaker(engine)
    lost = {"n": 1}

    class LostAck(SMClient):
        async def dispatch_action(self, *a, **kw):
            result = await super().dispatch_action(*a, **kw)
            if lost["n"]:
                lost["n"] -= 1
                raise ConnectionError("acknowledgement lost after acceptance")
            return result

    monkeypatch.setattr("harkeniq_cc.agent_runtime.SMClient", LostAck)
    monkeypatch.setattr("harkeniq_cc.api.approvals.SMClient", LostAck)
    try:
        app, state = _app(engine, maker, tenant, "owner")
        await seed_tenant_admin(maker, tenant, "kc-owner")
        await _estate(maker, tenant, sm_endpoint=sm.endpoint, node=sm.node)
        pid = (await agent_runtime.evaluate_agents(state, tenant))[0].id
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as c:
            res = await c.post(f"/api/approvals/{pid}/approve")
        assert res.json()["delivery"]["delivered"] is False
        first = await sm.directives_for(pid)
        assert len(first) == 1

        assert len(await agent_runtime.dispatch_decided(state, tenant)) == 1

        assert await sm.directives_for(pid) == first
        row = await _row(maker, tenant, pid)
        assert (row.status, row.directive_id) == ("dispatched", first[0][0])
        async with maker() as session:
            detail = (await session.execute(sa.text(
                "SELECT detail FROM cc_audit_log WHERE subject = :p AND "
                "action = 'agent_proposal.dispatched'"), {"p": pid})).scalar()
        assert detail["idempotent_replay"] is True
    finally:
        await _cleanup(maker, tenant)
        await engine.dispose()
        await sm.close()


# ---------------------------------------------------------------------------
# 6. The upgrades, on databases holding rows
# ---------------------------------------------------------------------------


def _alembic(service: str, dsn: str, *args: str) -> None:
    var = {"central_command": "HARKEN_CC_DSN", "site_manager": "HARKEN_SM_DSN"}[service]
    env = {k: v for k, v in os.environ.items() if not k.startswith("HARKEN_")}
    env[var] = dsn
    env["HARKEN_CC_TENANT_ID"] = "tenant-demo"
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO / "services" / service, env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


CC_INDEXES = {
    "ix_agent_proposals_tenant_status_created",
    "ix_agent_proposals_tenant_agent_created",
    "ix_agent_proposals_tenant_agent_basis_dispatched",
}


async def _index_validity(engine, table: str) -> dict:
    async with engine.connect() as conn:
        return dict((await conn.execute(sa.text(
            "SELECT c.relname, i.indisvalid FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indexrelid "
            "JOIN pg_class t ON t.oid = i.indrelid WHERE t.relname = :t"),
            {"t": table})).all())


async def test_cc_0028_to_0029_with_rows_and_an_invalid_index():
    tenant = f"pb0m-{uuid.uuid4().hex[:8]}"
    engine = make_engine(CC_DSN)
    maker = make_sessionmaker(engine)
    try:
        await _estate(maker, tenant)
        async with maker() as session:
            session.add_all([
                CCAgentProposal(
                    tenant_id=tenant, agent_id="m", actor="op-agent:m@v1",
                    agent_version=1, site_id="s", device_agent_id="n",
                    action_type="SEL_CLEAR", params={}, rationale="r", evidence={},
                    disposition="requires_approval",
                    authorization_basis="human_approval", status=status,
                    dedupe_key=f"m-{i}",
                ) for i, status in enumerate(
                    ["approved", "dispatched", "completed", "denied"] * 50)
            ])
            await session.commit()
        async with engine.connect() as conn:
            await conn.execute(sa.text(
                "DROP INDEX IF EXISTS ix_agent_proposals_tenant_agent_created"))
            await conn.execute(sa.text(
                "DROP INDEX IF EXISTS ix_agent_proposals_tenant_agent_basis_dispatched"))
            await conn.execute(sa.text(
                "UPDATE pg_index SET indisvalid = false WHERE indexrelid = "
                "'ix_agent_proposals_tenant_status_created'::regclass"))
            await conn.execute(sa.text(
                "ALTER TABLE cc_agent_proposals DROP COLUMN dispatch_claimed_at"))
            await conn.execute(sa.text("UPDATE alembic_version SET version_num = '0028'"))
            await conn.commit()
            before = (await conn.execute(sa.text(
                "SELECT id, status FROM cc_agent_proposals WHERE tenant_id = :t "
                "ORDER BY id"), {"t": tenant})).all()

        _alembic("central_command", CC_DSN, "upgrade", "head")

        valid = await _index_validity(engine, "cc_agent_proposals")
        assert CC_INDEXES <= set(valid) and all(valid[n] for n in CC_INDEXES), valid
        async with engine.connect() as conn:
            version = (await conn.execute(sa.text(
                "SELECT version_num FROM alembic_version"))).scalar()
            after = (await conn.execute(sa.text(
                "SELECT id, status FROM cc_agent_proposals WHERE tenant_id = :t "
                "ORDER BY id"), {"t": tenant})).all()
            claims = (await conn.execute(sa.text(
                "SELECT count(*) FROM cc_agent_proposals WHERE tenant_id = :t "
                "AND dispatch_claimed_at IS NOT NULL"), {"t": tenant})).scalar()
            typ = (await conn.execute(sa.text(
                "SELECT data_type FROM information_schema.columns WHERE "
                "table_name = 'cc_agent_proposals' AND column_name = "
                "'dispatch_claimed_at'"))).scalar()
        assert version == "0029"
        assert after == before and claims == 0
        assert typ == "timestamp with time zone"
        _alembic("central_command", CC_DSN, "upgrade", "head")        # idempotent
        _alembic("central_command", CC_DSN, "downgrade", "0028")
        valid = await _index_validity(engine, "cc_agent_proposals")
        assert not (CC_INDEXES & set(valid))
        _alembic("central_command", CC_DSN, "upgrade", "head")
    finally:
        _alembic("central_command", CC_DSN, "upgrade", "head")
        await _cleanup(maker, tenant)
        await engine.dispose()


@needs_sm
async def test_sm_0011_to_0012_with_historical_duplicates_and_an_invalid_index():
    sm = await _SM().start()
    try:
        async with sm.db() as session:
            for n in range(2):          # what a lost ack used to make
                await session.execute(sa.text(
                    "INSERT INTO sm_directives (id, device_id, kind, action_type, "
                    "skill_id, skill_version, tier, validation_state, issued_by, "
                    "actor, authorization_basis, proposal_id, status, "
                    "result_detail, created_at) VALUES (:id, :d, 'action', "
                    "'SEL_CLEAR', '', '', '', '', 'op', 'a', 'human_approval', "
                    ":p, 'completed', '', now())"),
                    {"id": uuid.uuid4().hex, "d": sm.device_id,
                     "p": f"dup-{sm.tag}"})
            await session.commit()
        async with sm.engine.connect() as conn:
            await conn.execute(sa.text(
                "UPDATE pg_index SET indisvalid = false WHERE indexrelid = "
                "'ux_sm_directives_idempotency_key'::regclass"))
            await conn.execute(sa.text("UPDATE alembic_version SET version_num = '0011'"))
            await conn.commit()

        _alembic("site_manager", SM_DSN, "upgrade", "head")

        valid = await _index_validity(sm.engine, "sm_directives")
        assert valid.get("ux_sm_directives_idempotency_key") is True
        dups = await sm.directives_for(f"dup-{sm.tag}")
        assert len(dups) == 2 and all(key is None for _id, key in dups), (
            "historical directives are never rewritten")
        # And the rebuilt index is a real unique index.
        first = await sm.call()
        again = await sm.call()
        assert again.directive_id == first.directive_id
        _alembic("site_manager", SM_DSN, "upgrade", "head")           # idempotent
        _alembic("site_manager", SM_DSN, "downgrade", "0011")
        assert "ux_sm_directives_idempotency_key" not in await _index_validity(
            sm.engine, "sm_directives")
    finally:
        _alembic("site_manager", SM_DSN, "upgrade", "head")
        await sm.close()
