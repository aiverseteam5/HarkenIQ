"""PB-1 (A30.42 D-PB2/D-PB4, A30.44) on a REAL PostgreSQL: the budget races.

sqlite shares one connection between sessions and cannot race. Here the
admitters and the dispatchers are separate engines -- separate replicas --
and the only thing that serializes them is the advisory lock each budget is
read under:

* admission: `cc.proposal_admission.{tenant}` -- lock -> count -> insert ->
  commit, for the evaluator and the ingress alike;
* unattended reservation: `cc.agent_execution.{tenant}.{agent}` -- lock ->
  count -> claim -> commit, before any Site Manager call.

Every race has a NON-VACUITY twin: the same race with the lock removed and a
barrier placed after the count must overspend, so the locked version's
"exactly N" is a property of the lock and not of the scheduler.

Gated on ``HARKEN_TEST_CC_PG_DSN`` (a database migrated to CC head).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa

from harkeniq_cc import agent_lifecycle, agent_runtime, proposal_admission
from harkeniq_cc.db.base import make_engine, make_sessionmaker
from harkeniq_cc.db.models import CCAgentProposal, CCOperationalAgent
from harkeniq_cc.db.repos import AgentProposalRepo
from harkeniq_cc.proposal_admission import (
    CODE_PROPOSAL_BUDGET_EXHAUSTED,
    ORIGIN_EVALUATOR,
    ORIGIN_INGRESS,
    admit_proposal,
    admitted_today,
)

CC_DSN = os.environ.get("HARKEN_TEST_CC_PG_DSN", "")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not CC_DSN, reason="HARKEN_TEST_CC_PG_DSN not set"),
]


async def _agent(maker, tenant: str, **values) -> str:
    async with maker() as session:
        agent = CCOperationalAgent(
            tenant_id=tenant, name=f"pb1-{uuid.uuid4().hex[:6]}",
            status="active", version=1, activated_version=1,
            created_by="pb1-test", **values,
        )
        session.add(agent)
        await session.commit()
        return agent.id


def _payload(tenant: str, agent_id: str, i: int, *, status="awaiting_approval",
             basis="human_approval") -> dict:
    return {
        "tenant_id": tenant, "agent_id": agent_id,
        "actor": f"op-agent:{agent_id}@v1", "agent_version": 1,
        "site_id": "pb1-site", "device_agent_id": f"pb1-node-{i}",
        "action_type": "SEL_CLEAR", "params": {"reason": str(i)},
        "rationale": "r", "evidence": {}, "disposition": "requires_approval",
        "disposition_reason": "", "blocking_conditions": [],
        "authorization_basis": basis, "status": status, "decided_by": "",
        "decided_at": None, "dedupe_key": f"{agent_id}:pb1-node-{i}:k{i}",
    }


async def _admit_one(maker, tenant, payload, origin=ORIGIN_EVALUATOR):
    async with maker() as session:
        row, code, _ = await admit_proposal(
            session, tenant_id=tenant, payload=payload, origin=origin,
        )
        await session.commit()
        return row is not None, code


async def _cleanup(maker, tenant: str):
    async with maker() as session:
        for stmt in (
            "DELETE FROM cc_agent_proposals WHERE tenant_id = :t",
            "DELETE FROM cc_operational_agents WHERE tenant_id = :t",
        ):
            await session.execute(sa.text(stmt), {"t": tenant})
        await session.commit()


def _barrier_before(monkeypatch, module, name: str, parties: int):
    barrier = asyncio.Barrier(parties)
    real = getattr(module, name)

    async def wrapped(*a, **kw):
        await barrier.wait()
        return await real(*a, **kw)

    monkeypatch.setattr(module, name, wrapped)


def _barrier_after(monkeypatch, module, name: str, parties: int):
    barrier = asyncio.Barrier(parties)
    real = getattr(module, name)

    async def wrapped(*a, **kw):
        out = await real(*a, **kw)
        await barrier.wait()
        return out

    monkeypatch.setattr(module, name, wrapped)


async def _no_lock(*_a, **_kw):
    return False


# ---------------------------------------------------------------------------
# 1. Two admitters race for the last slot
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("round_", range(5))
async def test_two_admitters_race_for_the_last_slot(round_, monkeypatch):
    tenant = f"pb1l-{uuid.uuid4().hex[:8]}"
    engines = [make_engine(CC_DSN), make_engine(CC_DSN)]
    makers = [make_sessionmaker(e) for e in engines]
    try:
        agent_id = await _agent(makers[0], tenant, max_proposals_per_day=2)
        assert (await _admit_one(makers[0], tenant, _payload(tenant, agent_id, 0)))[0]
        # Both arrive at the lock together; only one may take the last slot.
        _barrier_before(monkeypatch, proposal_admission,
                        "lock_proposal_admission", 2)
        results = await asyncio.gather(
            _admit_one(makers[0], tenant, _payload(tenant, agent_id, 1)),
            _admit_one(makers[1], tenant, _payload(tenant, agent_id, 2),
                       origin=ORIGIN_INGRESS),
        )
        assert sorted(r[0] for r in results) == [False, True], results
        assert CODE_PROPOSAL_BUDGET_EXHAUSTED in {r[1] for r in results}
        async with makers[0]() as session:
            assert await admitted_today(session, tenant, agent_id) == 2
    finally:
        await _cleanup(makers[0], tenant)
        for e in engines:
            await e.dispose()


async def test_without_the_lock_the_last_slot_is_spent_twice(monkeypatch):
    """Non-vacuity: remove the lock, hold both admitters after their count,
    and both take the last slot -- the race the lock exists to stop."""
    tenant = f"pb1n-{uuid.uuid4().hex[:8]}"
    engines = [make_engine(CC_DSN), make_engine(CC_DSN)]
    makers = [make_sessionmaker(e) for e in engines]
    try:
        agent_id = await _agent(makers[0], tenant, max_proposals_per_day=1)
        monkeypatch.setattr(proposal_admission, "lock_proposal_admission", _no_lock)
        _barrier_after(monkeypatch, proposal_admission, "admitted_today", 2)
        results = await asyncio.gather(
            _admit_one(makers[0], tenant, _payload(tenant, agent_id, 1)),
            _admit_one(makers[1], tenant, _payload(tenant, agent_id, 2)),
        )
        assert [r[0] for r in results] == [True, True]
    finally:
        await _cleanup(makers[0], tenant)
        for e in engines:
            await e.dispose()


# ---------------------------------------------------------------------------
# 2. A hundred concurrent admitters admit exactly N
# ---------------------------------------------------------------------------


async def test_a_hundred_concurrent_admitters_admit_exactly_n():
    tenant = f"pb1c-{uuid.uuid4().hex[:8]}"
    engines = [make_engine(CC_DSN) for _ in range(4)]
    makers = [make_sessionmaker(e) for e in engines]
    try:
        agent_id = await _agent(makers[0], tenant, max_proposals_per_day=7)
        results = await asyncio.gather(*(
            _admit_one(
                makers[i % 4], tenant, _payload(tenant, agent_id, i),
                origin=(ORIGIN_INGRESS if i % 2 else ORIGIN_EVALUATOR),
            )
            for i in range(100)
        ))
        assert sum(1 for ok, _ in results if ok) == 7
        assert {code for ok, code in results if not ok} == {
            CODE_PROPOSAL_BUDGET_EXHAUSTED,
        }
        async with makers[0]() as session:
            assert await admitted_today(session, tenant, agent_id) == 7
            assert (await session.execute(sa.select(sa.func.count(
                CCAgentProposal.id)).where(CCAgentProposal.tenant_id == tenant)
            )).scalar() == 7
    finally:
        await _cleanup(makers[0], tenant)
        for e in engines:
            await e.dispose()


async def test_blocked_rows_are_admitted_past_a_spent_budget():
    tenant = f"pb1b-{uuid.uuid4().hex[:8]}"
    engine = make_engine(CC_DSN)
    maker = make_sessionmaker(engine)
    try:
        agent_id = await _agent(maker, tenant, max_proposals_per_day=1)
        assert (await _admit_one(maker, tenant, _payload(tenant, agent_id, 0)))[0]
        assert (await _admit_one(
            maker, tenant, _payload(tenant, agent_id, 1, status="blocked")))[0]
        async with maker() as session:
            assert await admitted_today(session, tenant, agent_id) == 1
    finally:
        await _cleanup(maker, tenant)
        await engine.dispose()


# ---------------------------------------------------------------------------
# 3. Two replicas race for the last unattended unit
# ---------------------------------------------------------------------------


async def _autonomous_rows(maker, tenant: str, agent_id: str, n: int) -> list[str]:
    ids = []
    async with maker() as session:
        for i in range(n):
            row = CCAgentProposal(
                tenant_id=tenant, agent_id=agent_id,
                actor=f"op-agent:{agent_id}@v1", agent_version=1,
                site_id="pb1-site", device_agent_id=f"pb1-node-{i}",
                action_type="SEL_CLEAR", params={}, rationale="r", evidence={},
                disposition="autonomous", authorization_basis="autonomous_grant",
                status="approved", decided_by="autonomy:level-2",
                decided_at=datetime.now(timezone.utc),
                dedupe_key=f"{agent_id}:pb1-node-{i}:auto{i}",
            )
            session.add(row)
            await session.flush()
            ids.append(row.id)
        await session.commit()
    return ids


async def _reserve(maker, tenant: str, pid: str) -> bool:
    """The reservation half of `_dispatch_one`: count under the lock, then
    claim and commit. True = this replica reserved the unit."""
    async with maker() as session:
        repo = AgentProposalRepo(session)
        proposal = await repo.get(tenant, pid)
        allowed, _why = await agent_runtime._unattended_allowed(
            session, tenant, proposal,
        )
        if not allowed:
            await session.commit()
            return False
        claimed = await repo.claim_dispatch(proposal)
        await session.commit()
        return claimed


@pytest.mark.parametrize("round_", range(5))
async def test_two_replicas_race_for_the_last_unattended_unit(round_, monkeypatch):
    tenant = f"pb1u-{uuid.uuid4().hex[:8]}"
    engines = [make_engine(CC_DSN), make_engine(CC_DSN)]
    makers = [make_sessionmaker(e) for e in engines]
    try:
        agent_id = await _agent(
            makers[0], tenant, execution_budget=1, require_approval_always=False,
            autonomy_ceiling=2,
        )
        p1, p2 = await _autonomous_rows(makers[0], tenant, agent_id, 2)
        _barrier_before(monkeypatch, agent_lifecycle, "lock_unattended_budget", 2)
        results = await asyncio.gather(
            _reserve(makers[0], tenant, p1), _reserve(makers[1], tenant, p2),
        )
        assert sorted(results) == [False, True], results
        async with makers[0]() as session:
            agent = await session.get(CCOperationalAgent, agent_id)
            assert await agent_lifecycle.executions_used(session, tenant, agent) == 1
    finally:
        await _cleanup(makers[0], tenant)
        for e in engines:
            await e.dispose()


async def test_without_the_execution_lock_the_last_unit_is_spent_twice(monkeypatch):
    """Non-vacuity for the unattended lock."""
    tenant = f"pb1v-{uuid.uuid4().hex[:8]}"
    engines = [make_engine(CC_DSN), make_engine(CC_DSN)]
    makers = [make_sessionmaker(e) for e in engines]
    try:
        agent_id = await _agent(
            makers[0], tenant, execution_budget=1, require_approval_always=False,
            autonomy_ceiling=2,
        )
        p1, p2 = await _autonomous_rows(makers[0], tenant, agent_id, 2)
        monkeypatch.setattr(agent_lifecycle, "lock_unattended_budget", _no_lock)
        _barrier_after(monkeypatch, agent_lifecycle, "executions_used", 2)
        results = await asyncio.gather(
            _reserve(makers[0], tenant, p1), _reserve(makers[1], tenant, p2),
        )
        assert results == [True, True]
    finally:
        await _cleanup(makers[0], tenant)
        for e in engines:
            await e.dispose()


async def test_the_execution_lock_is_real_on_postgres():
    engine = make_engine(CC_DSN)
    try:
        async with make_sessionmaker(engine)() as session:
            assert await agent_lifecycle.lock_unattended_budget(
                session, "t", "a") is True
            assert await proposal_admission.lock_proposal_admission(
                session, "t") is True
            await session.rollback()
    finally:
        await engine.dispose()
