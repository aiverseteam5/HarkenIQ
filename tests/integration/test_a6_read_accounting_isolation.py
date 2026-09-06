"""A6-2 (LOW): durable read accounting owns its own transaction.

WHY THIS CANNOT BE A UNIT TEST, SAID PRECISELY.

`create_async_engine("sqlite+aiosqlite:///:memory:")` uses a StaticPool:
ONE connection, handed to every checkout. Two sessions opened against it
share a DBAPI connection and therefore share a transaction, so a commit
on either commits both. Reproduced directly, so this is a measured fact
rather than an assumption:

    async with sm() as s1:
        INSERT business            # not committed
        async with sm() as s2:
            INSERT accounting
            await s2.commit()      # commits BOTH rows
        await s1.rollback()        # discards nothing
    -> ['accounting', 'business']

A unit test asserting isolation there would fail whatever the code does,
and one asserting the sqlite behaviour would pin the wrong thing. So the
unit suite pins the property STRUCTURALLY -- `_charge_machine_read` takes
no caller session and cannot commit or roll back what it never received
-- and the durability is proved here, on the engine production runs.

The seven steps the remediation names:

  1. the caller has an uncommitted business mutation
  2. accounting executes
  3. accounting becomes durable
  4. the business mutation is still uncommitted
  5. the caller rolls back
  6. accounting survives
  7. the business mutation is gone

Gated on ``HARKEN_TEST_CC_PG_DSN``; skipped when unset.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
import sqlalchemy as sa

from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_cc.db.models import CCAgentReadWindow, CCSite
from harkeniq_cc.ingress_limits import (
    READ_MAX_PER_WINDOW, admit_read, read_window_start,
)

DSN = os.environ.get("HARKEN_TEST_CC_PG_DSN", "")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not DSN, reason="HARKEN_TEST_CC_PG_DSN not set"),
]

TENANT = "t-iso"


class _FakeUser:
    """Exactly the two fields the meter may read."""

    def __init__(self, tenant_id: str, user_id: str) -> None:
        self.tenant_id, self.user_id = tenant_id, user_id


class _FakeRequest:
    """The canonical session infrastructure, reached the way a route does."""

    def __init__(self, sessionmaker) -> None:
        cc = type("CC", (), {"sessionmaker": sessionmaker})()
        state = type("S", (), {"cc": cc})()
        self.app = type("A", (), {"state": state})()


async def _engine():
    engine = make_engine(DSN)
    await create_all(engine)
    return engine


async def _reads(sessionmaker, agent_id: str) -> int:
    async with sessionmaker() as session:
        return int(
            (await session.execute(
                sa.select(sa.func.coalesce(sa.func.sum(CCAgentReadWindow.reads), 0))
                .where(CCAgentReadWindow.agent_id == agent_id)
            )).scalar_one()
        )


@pytest.mark.asyncio
async def test_accounting_is_durable_while_the_caller_rolls_back():
    """The seven-step proof, on a real engine with real connections."""
    from harkeniq_cc.api.operational_agents import _charge_machine_read

    engine = await _engine()
    sm = make_sessionmaker(engine)
    agent_id = f"agent-{uuid.uuid4().hex[:8]}"
    site_name = f"business-{uuid.uuid4().hex[:8]}"
    request, user = _FakeRequest(sm), _FakeUser(TENANT, agent_id)
    try:
        async with sm() as caller:
            # 1. caller has an uncommitted business mutation
            caller.add(CCSite(
                tenant_id=TENANT, site_name=site_name,
                sm_endpoint="sm:1", sm_token="t",
            ))
            await caller.flush()

            # 2. accounting executes on its OWN session
            await _charge_machine_read(request, user)

            # 3. accounting is durable -- read from a THIRD connection,
            #    which can only see committed rows.
            assert await _reads(sm, agent_id) == 1

            # 4. the business mutation is still uncommitted: invisible to
            #    anyone but its own transaction.
            async with sm() as observer:
                seen = (await observer.execute(
                    sa.select(CCSite).where(CCSite.site_name == site_name)
                )).scalars().all()
            assert seen == [], (
                "the meter committed the caller's business work"
            )

            # 5. the caller rolls back
            await caller.rollback()

        # 6. accounting survives the rollback
        assert await _reads(sm, agent_id) == 1
        # 7. the business mutation is gone
        async with sm() as observer:
            gone = (await observer.execute(
                sa.select(CCSite).where(CCSite.site_name == site_name)
            )).scalars().all()
        assert gone == []
    finally:
        async with sm() as cleanup:
            await cleanup.execute(sa.delete(CCAgentReadWindow).where(
                CCAgentReadWindow.agent_id == agent_id))
            await cleanup.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_rate_refusal_is_also_durable_and_isolated():
    """A 429 is a refusal, and the charge that produced it must persist.

    Refusals were the hole: an authenticated caller whose refusal cost
    nothing has an unbounded channel. On the caller's own session that
    was already fragile -- the charge rode a transaction the handler
    might later abandon. On its own session it cannot.
    """
    from fastapi import HTTPException

    from harkeniq_cc.api.operational_agents import _charge_machine_read

    engine = await _engine()
    sm = make_sessionmaker(engine)
    agent_id = f"agent-{uuid.uuid4().hex[:8]}"
    window = read_window_start()
    request, user = _FakeRequest(sm), _FakeUser(TENANT, agent_id)
    try:
        async with sm() as session:
            session.add(CCAgentReadWindow(
                tenant_id=TENANT, agent_id=agent_id,
                window_start=window, reads=READ_MAX_PER_WINDOW,
            ))
            await session.commit()

        async with sm() as caller:
            caller.add(CCSite(
                tenant_id=TENANT, site_name=f"biz-{agent_id}",
                sm_endpoint="sm:1", sm_token="t",
            ))
            await caller.flush()
            with pytest.raises(HTTPException) as excinfo:
                await _charge_machine_read(request, user)
            assert excinfo.value.status_code == 429
            await caller.rollback()

        # The refusal was charged, and it outlived the caller's rollback.
        assert await _reads(sm, agent_id) == READ_MAX_PER_WINDOW + 1
        async with sm() as observer:
            assert (await observer.execute(
                sa.select(CCSite).where(CCSite.site_name == f"biz-{agent_id}")
            )).scalars().all() == []
    finally:
        async with sm() as cleanup:
            await cleanup.execute(sa.delete(CCAgentReadWindow).where(
                CCAgentReadWindow.agent_id == agent_id))
            await cleanup.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_reads_are_counted_exactly_once_each():
    """The SAVEPOINT contract A6-2 shipped, on the engine that needs it.

    On sqlite the window-open race cannot be observed at all: one writer,
    one connection. On PostgreSQL two replicas genuinely race to INSERT
    the first row of a window, and the loser must lose ONE statement --
    not its transaction, and not its count.
    """
    engine = await _engine()
    sm = make_sessionmaker(engine)
    agent_id = f"agent-{uuid.uuid4().hex[:8]}"
    concurrency = 8
    try:
        async def one():
            async with sm() as session:
                permitted, _ = await admit_read(
                    session, tenant_id=TENANT, agent_id=agent_id,
                )
                await session.commit()
                return permitted

        results = await asyncio.gather(*(one() for _ in range(concurrency)))
        assert all(results), "a read inside the allowance was refused"
        assert await _reads(sm, agent_id) == concurrency, (
            "concurrent reads were lost or double-counted"
        )
    finally:
        async with sm() as cleanup:
            await cleanup.execute(sa.delete(CCAgentReadWindow).where(
                CCAgentReadWindow.agent_id == agent_id))
            await cleanup.commit()
        await engine.dispose()
